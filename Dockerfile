# syntax=docker/dockerfile:1.7
#
# Two stages off the same pinned python:3.12-slim base: the builder has
# uv and resolves the locked dependency set into /app/.venv; the runtime
# stage copies only that venv, runs as an unprivileged user, and carries
# no build tooling. Bump the digests deliberately (Dependabot proposes).

ARG PYTHON_IMAGE=python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first so the layer cache survives code changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself, installed non-editable so the runtime stage
# needs only the venv.
COPY README.md ./
COPY smartthings_pushover/ ./smartthings_pushover/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM ${PYTHON_IMAGE}

# /config holds the client cert + key minted by SmartThings-Local's
# setup_cert.py (mount read-only). /data keeps the per-appliance cycle
# tracker so "Finished after 1h 12m" survives a restart.
RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /app --no-create-home app \
 && mkdir -p /app /config /data \
 && chown app:app /data

COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CERT_PATH=/config/client_fullchain.pem \
    KEY_PATH=/config/client.key \
    STATE_DIR=/data \
    HEARTBEAT_PATH=/tmp/smartthings-pushover.heartbeat

VOLUME ["/config", "/data"]
USER app
WORKDIR /app

# Outbound only: DTLS/UDP to the appliances, HTTPS to api.pushover.net.
# No ports exposed. Run with host networking (or a macvlan) so the
# container can reach the appliances' LAN segment and keep a stable
# local UDP port.

# Healthy = the main loop is still ticking. An unreachable appliance is
# *not* unhealthy (the bridge reconnects on its own); a wedged process is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["smartthings-pushover", "--healthcheck"]

CMD ["smartthings-pushover"]
