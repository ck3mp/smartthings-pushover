FROM python:3.12-slim

WORKDIR /app

# Dependencies first so the layer cache survives code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY smartthings_pushover/ ./smartthings_pushover/

# /config holds the client cert + key minted by SmartThings-Local's
# setup_cert.py. Mount it read-only; never bake secrets into the image.
RUN mkdir -p /config
VOLUME ["/config"]

ENV PYTHONUNBUFFERED=1 \
    CERT_PATH=/config/client_fullchain.pem \
    KEY_PATH=/config/client.key

# Outbound only: DTLS/UDP to the washer, HTTPS to api.pushover.net.
# No ports exposed. Needs host networking (or a macvlan) if your NAS's
# bridge network can't reach the appliance's LAN segment.

CMD ["python", "-m", "smartthings_pushover"]
