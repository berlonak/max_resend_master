# copyright by berlonak
# telegram: @Kilax123
"""Загрузка корневых сертификатов НУЦ Минцифры России.

MAX использует TLS-сертификаты Национального удостоверяющего центра Минцифры.
Их корневого сертификата нет ни в операционных системах, ни в наборе certifi,
поэтому проверка падает с 'unable to get local issuer certificate'.

Скрипт кладёт сертификаты в папку certs/ рядом с проектом — мост подхватывает
их автоматически, ничего в системе менять не нужно.

Запуск:  python install_ru_certs.py

Официальный источник — портал Госуслуг: https://www.gosuslugi.ru/crt
Скрипт качает файлы с сайта gu-st.ru, на который ссылается портал.
"""
from __future__ import annotations

import os
import ssl
import sys
import urllib.request

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")

SOURCES = [
    ("russian_trusted_root_ca.cer",
     ["https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
      "https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer"]),
    ("russian_trusted_sub_ca.cer",
     ["https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
      "https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer"]),
]


def looks_like_certificate(data: bytes) -> bool:
    if b"-----BEGIN CERTIFICATE-----" in data:
        return True
    # DER всегда начинается с SEQUENCE (0x30) и имеет разумный размер
    return len(data) > 300 and data[:1] == b"\x30"


def download(urls: list[str]) -> bytes | None:
    """Качает первый доступный адрес.

    Проверка сертификата здесь намеренно отключена: мы находимся в ситуации
    'нет доверенного корня', то есть именно её и решаем. Целостность файла
    проверяем отдельно — по содержимому и по тому, что он реально работает.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for url in urls:
        try:
            print(f"   пробую {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "max-tg-bridge"})
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                data = r.read()
            if looks_like_certificate(data):
                return data
            print("   ответ не похож на сертификат, пробую следующий адрес")
        except Exception as e:
            print(f"   не вышло: {type(e).__name__}: {e}")
    return None


def main() -> int:
    print("Установка корневых сертификатов НУЦ Минцифры России\n")
    os.makedirs(CERT_DIR, exist_ok=True)

    saved = []
    for name, urls in SOURCES:
        print(f"→ {name}")
        data = download(urls)
        if not data:
            print(f"   ❌ скачать не удалось\n")
            continue
        path = os.path.join(CERT_DIR, name)
        with open(path, "wb") as f:
            f.write(data)
        kind = "PEM" if b"-----BEGIN" in data else "DER"
        print(f"   ✅ сохранён: {path}  ({len(data)} байт, {kind})\n")
        saved.append(path)

    if not saved:
        print("Ничего скачать не удалось. Скачайте сертификаты вручную:")
        print("  https://www.gosuslugi.ru/crt")
        print(f"и положите файлы в папку:\n  {CERT_DIR}")
        return 1

    # Проверяем, что теперь соединение с MAX действительно проходит
    print("Проверяю соединение с platform-api2.max.ru...")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sslsetup
        ctx = sslsetup.build_ssl_context()
        ok, detail = sslsetup.verify_host("platform-api2.max.ru", ctx=ctx)
        print(("✅ " if ok else "❌ ") + detail)
        if ok:
            print("\nГотово. Запускайте  python main.py")
        else:
            print("\nСертификаты сохранены, но проверка пока не проходит.")
            print("Запустите  python diagnose.py  — он покажет, кто выдал сертификат.")
            return 1
    except Exception as e:
        print(f"проверить не удалось: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
