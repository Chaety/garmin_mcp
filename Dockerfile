FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN uv pip install -e . && \
    uv pip install "garminconnect @ git+https://github.com/cyberjunky/python-garminconnect@react"

COPY tests/ ./tests/
COPY pytest.ini ./

RUN mkdir -p /root/.garminconnect && \
    chmod 700 /root/.garminconnect

RUN printf '#!/bin/sh\nif [ -n "$GARMIN_TOKENS" ]; then\n    mkdir -p /root/.garminconnect\n    echo "$GARMIN_TOKENS" > /root/.garminconnect/garmin_tokens.json\nfi\nexec garmin-mcp "$@"\n' > /entrypoint.sh && \
    chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
