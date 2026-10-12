"""Загрузка файлов из интернета для обновляемых справочников (правила Sigma, MITRE ATT&CK).

Единое место для проверки сертификатов, ограничения размера и понятных сообщений об ошибках.
Прокси берётся из переменных окружения HTTPS_PROXY / NO_PROXY (их читает urllib), свой корневой
сертификат прокси задаёт SSL_CERT_FILE.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import ssl
import urllib.error
import urllib.request

UA = "LogAtlas"


def ssl_context() -> ssl.SSLContext:
    """Проверка сертификатов: системное хранилище, а если оно пусто (встроенный Python приложения
    для macOS) — certifi или /etc/ssl/cert.pem."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca") or os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if os.path.isfile("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


def download(url: str, max_bytes: int, on_progress=None, timeout: int = 60) -> bytes:
    """Скачать файл. on_progress(получено, всего) — для показа хода; всего = 0, если сервер не сказал.
    Сжатие при передаче включено: справочник ATT&CK занимает 54 МБ, а по сети идёт около 6 МБ."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        total = int(r.headers.get("Content-Length") or 0)
        gz = (r.headers.get("Content-Encoding") or "").lower() == "gzip"
        if on_progress:
            on_progress(0, total)
        while True:
            chunk = r.read(256 << 10)
            if not chunk:
                break
            buf.write(chunk)
            if on_progress:
                on_progress(buf.tell(), total)
            # сжатый поток считаем по переданному объёму, несжатый — по нему же; распакованное
            # проверяем ниже, чтобы маленький архив не развернулся в огромный файл
            if buf.tell() > max_bytes:
                raise RuntimeError(f"Файл больше {max_bytes >> 20} МБ — загрузка остановлена")
    data = buf.getvalue()
    if gz:
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                if out.tell() > max_bytes * 16:
                    raise RuntimeError("Распакованный файл неправдоподобно большой — загрузка остановлена")
        data = out.getvalue()
    return data


def host_of(url: str) -> str:
    return re.sub(r"^https?://([^/]+).*", r"\1", url)


def error_text(e: Exception, url: str, offline_hint: str = "") -> str:
    """Сообщение об ошибке загрузки понятными словами."""
    host = host_of(url)
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return f"На {host} нет такого файла (HTTP 404)."
        if e.code in (403, 429):
            return f"{host} временно ограничил загрузки (HTTP {e.code}) — попробуйте позже. {offline_hint}".strip()
        return f"{host} ответил ошибкой HTTP {e.code}. {offline_hint}".strip()
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(e, ssl.SSLCertVerificationError):
        return (f"Не удалось проверить сертификат {host}: {getattr(reason, 'verify_message', reason)}. "
                "Если в сети прокси с собственным корневым сертификатом — укажите его в переменной SSL_CERT_FILE.")
    return f"Нет связи с {host}: {reason}. {offline_hint}".strip()
