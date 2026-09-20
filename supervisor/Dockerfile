# The wacli binary, built in the same multi-stage step as the web service's.
FROM golang:1.27-bookworm AS wacli-builder
ENV CGO_ENABLED=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN go install github.com/openclaw/wacli/cmd/wacli@latest

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=wacli-builder /go/bin/wacli /usr/local/bin/wacli

WORKDIR /app

# The shared package (app.db, providers, ...); build context is the repo root.
COPY shared ./shared
RUN pip install --upgrade pip && pip install ./shared

COPY supervisor/supervisor.py ./supervisor.py

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "supervisor.py"]
