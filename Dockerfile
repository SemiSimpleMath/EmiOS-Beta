# EmiOS — Docker image
# Builds a self-contained container for running EmiOS with any OpenAI-compatible
# provider (including OpenCode Go).
#
# Build:
#   docker compose build
#
# Run:
#   docker compose up -d
#   # Visit http://<host>:8000 and walk the setup wizard.
#   # After the wizard, stop, add your API key to .env, and restart.

FROM python:3.11-slim

LABEL org.opencontainers.image.title="EmiOS"
LABEL org.opencontainers.image.description="Local-first personal AI assistant"
LABEL org.opencontainers.image.source="https://github.com/SemiSimpleMath/EmiOS-Beta"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ---- system dependencies ------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        libxml2-dev \
        libxslt1-dev \
        lsof \
        curl \
        libgomp1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ---- create non-root user ------------------------------------------------
RUN groupadd -r emi && useradd -r -g emi -d /app -s /bin/false emi

WORKDIR /app

# ---- copy project files --------------------------------------------------
COPY --chown=emi:emi app app
COPY --chown=emi:emi belief_engine belief_engine
COPY --chown=emi:emi configs configs
COPY --chown=emi:emi docs docs
COPY --chown=emi:emi docker docker
COPY --chown=emi:emi migrations migrations
COPY --chown=emi:emi mcp mcp
COPY --chown=emi:emi resources resources
COPY --chown=emi:emi scripts scripts
COPY --chown=emi:emi skills skills
COPY --chown=emi:emi sqlite_utilities sqlite_utilities
COPY --chown=emi:emi tasks tasks
COPY --chown=emi:emi work_objects work_objects
COPY --chown=emi:emi config.py setup.py start.py run_flask.py requirements.txt VERSION .env.example ./
COPY --chown=emi:emi emi.command CLAUDE.md EXTENDING.md FEATURES.md ./

# ---- create venv and install dependencies --------------------------------
RUN python3 -m venv .venv && \
    .venv/bin/pip install --upgrade pip && \
    .venv/bin/pip install -r requirements.txt

# ---- create required directories -----------------------------------------
# emi's home is /app (useradd -d /app), so huggingface/chroma resolve their
# cache to /app/.cache — which did not exist and is not creatable by a
# non-root user, degrading the embedder at startup. Create it explicitly and
# pin XDG_CACHE_HOME so the location does not depend on HOME.
RUN mkdir -p logs uploads instance chroma_db app/personal_info data && \
    chown -R emi:emi logs uploads instance chroma_db app/personal_info data \
    && mkdir -p /.cache /app/.cache && chown -R emi:emi /.cache /app/.cache
ENV XDG_CACHE_HOME=/app/.cache

# ---- data persistence dir -------------------------------------------------
RUN mkdir -p /data && chown -R emi:emi /data

# ---- default environment -------------------------------------------------
# Infrastructure only. Do NOT bake provider/model/credentials in here:
# run_flask.py calls load_dotenv() on $EMI_DATA_DIR/.env, and load_dotenv does
# not override variables that are already set. An ENV here therefore shadows
# the user's /data/.env permanently and cannot be corrected by the setup
# wizard — which is how every agent ended up pinned to provider=openai with
# no OPENAI_API_KEY. Provider/model belong in /data/.env.
ENV EMI_DATA_DIR=/data
ENV EMI_HOST=0.0.0.0
ENV EMI_PORT=8000

EXPOSE 8000

USER emi

# Bypass the venv-enforcement check (in Docker we know we're using the right
# Python) and the port-conflict check (clean container), then start Flask.
# run_flask.py loads .env from $EMI_DATA_DIR/.env if set.
# Use CMD so `docker run ...` can override with a different command for debugging
CMD [".venv/bin/python", "run_flask.py"]
