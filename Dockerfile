# Multi-stage build for the AI SRE Copilot web UI (ai_platform.webui.server).
#
# Runs the platform as a container instead of a bare `uv run` host process —
# see milestones.md's "Production deployment" phase-1 note. Doesn't bundle
# the Kind cluster or the OpenTelemetry Demo app itself: this image is just
# ai_platform, which expects PROMETHEUS_URL/JAEGER_URL/KUBERNETES_NAMESPACE
# (see .env.example) to point at a reachable cluster, in-cluster or not.

# --- Build stage -------------------------------------------------------------
# python:3.14-slim matches .python-version / pyproject.toml's
# requires-python = ">=3.14" exactly, so the editable-install/package
# metadata resolution behaves the same as local dev.
FROM python:3.14-slim AS builder

# pip-installing uv rather than pulling astral's separate uv image keeps
# this to one base image family; the extra install cost only hits the
# builder stage, never the runtime image below.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency manifests first so `uv sync` is cached across rebuilds
# that only change application code, not dependencies. README.md is needed
# here too — hatchling's [project] readme = "README.md" makes the build
# metadata step fail without it, even though the file itself has no
# runtime purpose in the image.
COPY pyproject.toml uv.lock README.md ./

# --no-dev: skips pytest/httpx (ai_platform/*/tests/ still run in CI, not
# inside the shipped image). --frozen: fail rather than silently
# re-resolving if uv.lock is out of date with pyproject.toml.
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the actual package and finish the install (the editable install
# step needs the source present — see pyproject.toml's [tool.uv]
# package = true comment on why this makes `ai_platform.x.y` imports work
# from anywhere).
COPY ai_platform ./ai_platform
RUN uv sync --frozen --no-dev

# --- Runtime stage -------------------------------------------------------
FROM python:3.14-slim AS runtime

# curl: used by the HEALTHCHECK below to hit /healthz (server.py). Kept to
# just this one package rather than the full builder-stage toolchain.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY ai_platform ./ai_platform
COPY pyproject.toml README.md ./

# knowledge_base.py's DEFAULT_KB_DIR resolves to <repo root>/knowledge_base
# relative to ai_platform/tools/ at import time — copied here to the same
# relative position (/app/knowledge_base, since ai_platform lands at
# /app/ai_platform) so search_knowledge_base finds real content instead of
# silently returning zero results in the container.
COPY knowledge_base ./knowledge_base

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Runs as non-root — the container never needs to write anywhere except
# wherever WEBUI_DB_PATH/incidents.db and ai_platform/runbooks/ are mounted
# (see the docker-compose/K8s notes in docs/ once those exist); root isn't
# needed for a plain "listen on a port and call out to Prometheus/Jaeger/K8s"
# process.
RUN useradd --create-home --uid 1000 copilot \
    && chown -R copilot:copilot /app
USER copilot

# WEBUI_HOST defaults to 127.0.0.1 in server.py (loopback-only) — that's
# wrong inside a container, where the health check and any external
# traffic come from outside the container's own network namespace. Set
# 0.0.0.0 here as the image default; .env/deployment config can still
# override it, but a container that only listens on its own loopback is a
# guaranteed-broken default, not a safe one.
ENV WEBUI_HOST=0.0.0.0

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["python", "-m", "ai_platform.webui.server"]
