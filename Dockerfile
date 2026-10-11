# LogAtlas — анализатор журналов macOS / Windows / Linux
# Готовые образы для linux/amd64 и linux/arm64: ghcr.io/arsis-02/logatlas (см. README).
# Сборка на месте — под архитектуру хоста: docker compose up -d --build
FROM python:3.12-slim

# UID/GID пользователя в образе (ssh требует запись о нём в /etc/passwd). Готовый образ собран
# с 1000; при запуске от другого UID запись добавляет docker/entrypoint.sh (nss_wrapper).
ARG LOGATLAS_UID=1000
ARG LOGATLAS_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOGATLAS_DATA=/data \
    LOGATLAS_INBOX=/data/inbox \
    TMPDIR=/data/tmp

# openssh-client — сбор журналов с хостов по SSH из панели; libnss-wrapper — см. docker/entrypoint.sh
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client libnss-wrapper \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# evtx (быстрый парсер EVTX на Rust) ставим только готовым колесом;
# если под платформу его нет — берём python-evtx (чистый Python, медленнее).
RUN pip install --no-cache-dir -r requirements.txt \
 && (pip install --no-cache-dir --only-binary=:all: evtx || pip install --no-cache-dir python-evtx)

COPY app ./app
COPY static ./static
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/logatlas-entrypoint
RUN chmod -R a+rX /app && chmod 755 /usr/local/bin/logatlas-entrypoint

RUN (getent group "$LOGATLAS_GID" >/dev/null || groupadd -g "$LOGATLAS_GID" logatlas) \
 && (getent passwd "$LOGATLAS_UID" >/dev/null || useradd -u "$LOGATLAS_UID" -g "$LOGATLAS_GID" -d /data -M -s /usr/sbin/nologin logatlas) \
 && mkdir -p /data/inbox /data/tmp && chmod -R 0777 /data
VOLUME ["/data"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=4)" || exit 1

LABEL org.opencontainers.image.title="LogAtlas" \
      org.opencontainers.image.description="Анализатор журналов macOS, Windows и Linux" \
      org.opencontainers.image.source="https://github.com/ArsIS-02/logatlas" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/usr/local/bin/logatlas-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8484", "--timeout-keep-alive", "30"]
