FROM nousresearch/hermes-agent:latest

USER root

COPY sidecar.py /opt/hermes-sidecar/sidecar.py
COPY start.sh /opt/hermes-sidecar/start.sh
RUN chmod +x /opt/hermes-sidecar/start.sh

ENV HERMES_DASHBOARD_PORT=8080
ENV API_SERVER_HOST=127.0.0.1
ENV API_SERVER_PORT=8642
ENV API_SERVER_ENABLED=true

EXPOSE 8080

ENTRYPOINT ["/opt/hermes-sidecar/start.sh"]
