# The wacli binary, built in the same multi-stage step as the supervisor's.
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

# The build context is the repo root (see build.context in docker-compose.yml).
COPY shared ./shared
COPY web/requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
    && pip install ./shared \
    && pip install -r requirements.txt

COPY web/app ./app

# A neutral default HOME; the real store is addressed per user in code.
ENV HOME=/wacli-state

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
# --proxy-headers: behind a tunnel or reverse proxy, X-Forwarded-Proto is how
# the app learns it is really serving https. The port is bound to loopback
# on the host, so trusting every peer here is trusting the proxy in front.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
