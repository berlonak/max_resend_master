# copyright by berlonak
# telegram: @Kilax123
"""Настройка TLS для запросов к MAX.

Зачем нужно
-----------
MAX использует TLS-сертификаты НУЦ Минцифры России. Их корневой сертификат не
входит в поставку операционных систем и в набор certifi, поэтому проверка
падает с ошибкой:

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

Отдельная тонкость: httpx по умолчанию доверяет только набору certifi и НЕ
смотрит в хранилище Windows. Поэтому даже установленный в систему сертификат
Минцифры сам по себе не помогает — нужно собрать контекст явно.

Что делает модуль
-----------------
Складывает в один контекст доверия:
  1. системное хранилище (в Windows — хранилища CA и ROOT самой Windows);
  2. набор certifi, если он установлен;
  3. все сертификаты из папки certs/ рядом с проектом;
  4. дополнительный файл из переменной MAX_CA_BUNDLE.

Так работает и сертификат Минцифры, и корневой сертификат корпоративного
прокси или антивируса, если тот вскрывает TLS.
"""
from __future__ import annotations

import glob
import os
import socket
import ssl

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")

# Приметы в сертификате, по которым понятно, кто его выдал.
_ISSUER_HINTS = {
    b"Russian Trusted": "НУЦ Минцифры России (Russian Trusted CA)",
    b"Ministry of Digital": "Минцифры России",
    b"Kaspersky": "Kaspersky — антивирус вскрывает TLS",
    b"ESET": "ESET — антивирус вскрывает TLS",
    b"Avast": "Avast — антивирус вскрывает TLS",
    b"AVG": "AVG — антивирус вскрывает TLS",
    b"Bitdefender": "Bitdefender — антивирус вскрывает TLS",
    b"Dr.Web": "Dr.Web — антивирус вскрывает TLS",
    b"Fiddler": "Fiddler — перехватчик трафика",
    b"Charles": "Charles Proxy — перехватчик трафика",
    b"Burp": "Burp Suite — перехватчик трафика",
    b"Proxifier": "Proxifier",
    b"Zscaler": "Zscaler — корпоративный прокси",
    b"NetSkope": "Netskope — корпоративный прокси",
}


def _load_cert_file(ctx: ssl.SSLContext, path: str) -> bool:
    """Загружает сертификат в контекст. Понимает и PEM, и бинарный DER."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if b"-----BEGIN CERTIFICATE-----" in data:
            ctx.load_verify_locations(cadata=data.decode("ascii", "ignore"))
        else:
            ctx.load_verify_locations(cadata=data)      # DER
        return True
    except Exception:
        return False


def build_ssl_context(extra_ca: str | None = None, verify: bool = True):
    """Готовит объект для параметра verify= в httpx.

    verify=False возвращает False — проверка отключается (только для отладки).
    """
    if not verify:
        return False

    # На Windows create_default_context() подтягивает хранилища CA и ROOT системы.
    ctx = ssl.create_default_context()

    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass

    if os.path.isdir(CERT_DIR):
        for pattern in ("*.crt", "*.cer", "*.pem"):
            for path in sorted(glob.glob(os.path.join(CERT_DIR, pattern))):
                _load_cert_file(ctx, path)

    if extra_ca:
        if os.path.isdir(extra_ca):
            try:
                ctx.load_verify_locations(capath=extra_ca)
            except Exception:
                pass
        elif os.path.isfile(extra_ca):
            _load_cert_file(ctx, extra_ca)

    return ctx


def loaded_cert_count(ctx: ssl.SSLContext) -> int:
    try:
        return len(ctx.get_ca_certs())
    except Exception:
        return 0


def local_cert_files() -> list[str]:
    if not os.path.isdir(CERT_DIR):
        return []
    out: list[str] = []
    for pattern in ("*.crt", "*.cer", "*.pem"):
        out += glob.glob(os.path.join(CERT_DIR, pattern))
    return sorted(out)


def fetch_server_cert(host: str, port: int = 443, timeout: float = 15.0) -> bytes | None:
    """Забирает сертификат сервера БЕЗ проверки — чтобы посмотреть, кто его выдал."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return tls.getpeercert(binary_form=True)
    except Exception:
        return None


def describe_issuer(der: bytes) -> str:
    """Определяет издателя сертификата.

    Сначала пробует библиотеку cryptography, если она установлена;
    иначе ищет характерные названия прямо в байтах сертификата.
    """
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        return cert.issuer.rfc4514_string()
    except Exception:
        pass

    for marker, label in _ISSUER_HINTS.items():
        if marker in der:
            return label
    return "не удалось определить (установите пакет cryptography для подробностей)"


def verify_host(host: str, port: int = 443, ctx=None, timeout: float = 15.0) -> tuple[bool, str]:
    """Пробует установить TLS-соединение с проверкой. Возвращает (успех, пояснение)."""
    context = ctx if isinstance(ctx, ssl.SSLContext) else ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host):
                return True, "проверка сертификата пройдена"
    except ssl.SSLCertVerificationError as e:
        return False, f"сертификат не прошёл проверку: {e.verify_message or e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
