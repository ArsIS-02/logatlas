# LogAtlas — анализатор журналов macOS / Windows / Linux
# Собирается под архитектуру хоста (на Raspberry Pi — linux/arm64).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOGATLAS_DATA=/data \
    LOGATLAS_INBOX=/data/inbox \
    TMPDIR=/data/tmp

WORKDIR /app
COPY requirements.txt .
# evtx (быстрый парсер EVTX на Rust) ставим только готовым колесом;
# если под платформу его нет — берём python-evtx (чистый Python, медленнее).
RUN pip install --no-cache-dir -r requirements.txt \
 && (pip install --no-cache-dir --only-binary=:all: evtx || pip install --no-cache-dir python-evtx)

COPY app ./app
COPY static ./static

RUN mkdir -p /data/inbox /data/tmp && chmod -R 0777 /data
VOLUME ["/data"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8484", "--timeout-keep-alive", "30"]
