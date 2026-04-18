"""
Hermes dashboard sidecar.

Single public-facing aiohttp app on 0.0.0.0:$HERMES_DASHBOARD_PORT that:

  * Serves identity/memory file CRUD under `/files`, `/files/{name}` —
    scoped to a hard-coded allowlist inside `$HERMES_HOME`.
  * Reverse-proxies every other path to the local Hermes api_server on
    127.0.0.1:$API_SERVER_PORT, including SSE streams.

Auth: every route requires `Authorization: Bearer $API_SERVER_KEY` —
the same token used by the upstream Hermes API server.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data")).resolve()
DASHBOARD_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "8080"))
API_SERVER_HOST = os.environ.get("API_SERVER_HOST", "127.0.0.1")
API_SERVER_PORT = int(os.environ.get("API_SERVER_PORT", "8642"))
API_SERVER_KEY = os.environ.get("API_SERVER_KEY", "")
UPSTREAM = f"http://{API_SERVER_HOST}:{API_SERVER_PORT}"

# Paths the dashboard is allowed to read/write, relative to HERMES_HOME.
# Anything outside this set is rejected even if the bearer token is valid.
FILE_ALLOWLIST = {
    "SOUL.md": HERMES_HOME / "SOUL.md",
    "MEMORY.md": HERMES_HOME / "memories" / "MEMORY.md",
    "USER.md": HERMES_HOME / "memories" / "USER.md",
    "AGENTS.md": HERMES_HOME / "AGENTS.md",
    "HERMES.md": HERMES_HOME / "HERMES.md",
}

MAX_FILE_BYTES = 1_000_000  # 1 MB upper bound for any single file write.


def _check_auth(request: web.Request) -> Optional[web.Response]:
    if not API_SERVER_KEY:
        return web.json_response({"error": "API_SERVER_KEY not configured"}, status=500)
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return web.json_response({"error": "Missing bearer token"}, status=401)
    presented = header[len("Bearer "):]
    if not hmac.compare_digest(presented, API_SERVER_KEY):
        return web.json_response({"error": "Invalid token"}, status=401)
    return None


# ── Files ──────────────────────────────────────────────────────────────


async def list_files(request: web.Request) -> web.Response:
    err = _check_auth(request)
    if err:
        return err

    files = []
    for name, path in FILE_ALLOWLIST.items():
        info = {"name": name, "path": str(path), "exists": path.is_file()}
        if info["exists"]:
            stat = path.stat()
            info["size"] = stat.st_size
            info["modified"] = stat.st_mtime
        files.append(info)
    return web.json_response({"files": files})


async def get_file(request: web.Request) -> web.Response:
    err = _check_auth(request)
    if err:
        return err

    name = request.match_info["name"]
    path = FILE_ALLOWLIST.get(name)
    if path is None:
        return web.json_response({"error": f"File not allowed: {name}"}, status=404)
    if not path.is_file():
        return web.json_response({"name": name, "content": "", "exists": False})
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return web.json_response({"error": "File is not UTF-8"}, status=415)
    return web.json_response({"name": name, "content": content, "exists": True})


async def put_file(request: web.Request) -> web.Response:
    err = _check_auth(request)
    if err:
        return err

    name = request.match_info["name"]
    path = FILE_ALLOWLIST.get(name)
    if path is None:
        return web.json_response({"error": f"File not allowed: {name}"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response({"error": "'content' must be a string"}, status=400)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return web.json_response({"error": "File too large"}, status=413)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return web.json_response({"name": name, "ok": True, "size": len(content.encode("utf-8"))})


# ── Reverse proxy ──────────────────────────────────────────────────────


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


async def proxy(request: web.Request) -> web.StreamResponse:
    # Auth is enforced upstream by Hermes' api_server on the same key, so
    # we don't double-check here — but we *do* refuse if the header is missing
    # to avoid leaking 401s without a clear error.
    if "Authorization" not in request.headers:
        return web.json_response({"error": "Missing Authorization header"}, status=401)

    target_url = f"{UPSTREAM}{request.rel_url}"
    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    body = await request.read() if request.can_read_body else None

    session: aiohttp.ClientSession = request.app["client"]
    upstream_resp = await session.request(
        method=request.method,
        url=target_url,
        headers=forward_headers,
        data=body,
        allow_redirects=False,
    )

    response = web.StreamResponse(status=upstream_resp.status, reason=upstream_resp.reason)
    for k, v in upstream_resp.headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        response.headers[k] = v
    await response.prepare(request)

    try:
        async for chunk in upstream_resp.content.iter_any():
            if not chunk:
                continue
            await response.write(chunk)
    finally:
        upstream_resp.release()

    return response


# ── App ────────────────────────────────────────────────────────────────


async def on_startup(app: web.Application) -> None:
    app["client"] = aiohttp.ClientSession()


async def on_cleanup(app: web.Application) -> None:
    await app["client"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/files", list_files)
    app.router.add_get("/files/{name}", get_file)
    app.router.add_put("/files/{name}", put_file)
    # Catch-all reverse proxy for everything else.
    app.router.add_route("*", "/{tail:.*}", proxy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=DASHBOARD_PORT, access_log=None)
