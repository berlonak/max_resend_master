# copyright by berlonak
# telegram: @Kilax123
"""Диагностика окружения. Проверяет, может ли asyncio вообще запуститься.

Запуск:  python diagnose.py

Скрипт не трогает код моста и не требует токенов — проверяется только Python
и сеть машины.
"""
from __future__ import annotations

import os
import platform
import socket
import sys


def line(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def check_python() -> None:
    line("1. Python и система")
    print(f"Версия Python : {sys.version.split()[0]}")
    print(f"Путь          : {sys.executable}")
    print(f"ОС            : {platform.system()} {platform.release()}")
    if os.name == "nt":
        print("Системного socketpair() на Windows нет — asyncio эмулирует его через TCP.")


def check_socketpair() -> bool:
    line("2. Штатный socket.socketpair() — критическая проверка")
    try:
        ssock, csock = socket.socketpair()
        print("✅ Работает")
        try:
            s_name, c_peer = ssock.getsockname(), csock.getpeername()
            if s_name and c_peer:
                print(f"   сервер {s_name} <- клиент {c_peer}")
        except OSError:
            pass
        ssock.close()
        csock.close()
        return True
    except Exception as e:
        print(f"❌ Не работает: {type(e).__name__}: {e}")
        print("\n   Это и есть причина падения main.py: asyncio создаёт внутренний")
        print("   'self-pipe' через socketpair, поэтому цикл событий не создаётся.")
        _show_addresses()
        return False


def _show_addresses() -> None:
    """Показывает реальные адреса сторон — видно подмену."""
    print("\n   Что происходит на самом деле:")
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lsock.bind(("127.0.0.1", 0))
        lsock.listen()
        lsock.settimeout(10)
        addr, port = lsock.getsockname()[:2]
        csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        csock.settimeout(10)
        csock.connect((addr, port))
        ssock, _ = lsock.accept()
        try:
            print(f"   слушающий сокет        : {ssock.getsockname()}")
            print(f"   куда подключился клиент: {csock.getpeername()}")
            print(f"   клиентский сокет       : {csock.getsockname()}")
            print(f"   кого видит сервер      : {ssock.getpeername()}")
            if (ssock.getsockname() != csock.getpeername()
                    or csock.getsockname() != ssock.getpeername()):
                print("\n   ⚠️  Адреса НЕ совпадают — соединения к localhost перехватываются.")
        finally:
            ssock.close()
            csock.close()
    except Exception as e:
        print(f"   проверить не удалось: {e}")
    finally:
        lsock.close()


def check_strategies() -> bool:
    line("3. Перебор способов создать socketpair")
    try:
        import winfix
    except ImportError:
        print("Файл winfix.py не найден рядом со скриптом.")
        return False

    results = winfix.probe()
    for name, works in results:
        print(f"   {'✅' if works else '❌'}  {name}")

    working = [n for n, w in results if w]
    if not working:
        print("\n❌ Ни один способ не работает.")
        return False
    print(f"\n✅ Рабочих способов: {len(working)}. Будет выбран «{working[0]}».")
    return True


def check_asyncio() -> bool:
    line("4. Запуск asyncio (как в main.py)")
    try:
        import winfix
        winfix.apply(verbose=False)
        import asyncio

        async def hello() -> str:
            await asyncio.sleep(0)
            return "ok"

        asyncio.run(hello())
        used = winfix.strategy()
        print(f"✅ asyncio запускается" + (f" (способ: {used})" if used else " (патч не понадобился)"))
        return True
    except Exception as e:
        print(f"❌ asyncio не запускается: {type(e).__name__}: {e}")
        return False



MAX_HOST = "platform-api2.max.ru"


def check_tls() -> bool:
    line(f"5. TLS-соединение с {MAX_HOST}")
    try:
        import sslsetup
    except ImportError:
        print("Файл sslsetup.py не найден рядом со скриптом.")
        return False

    der = sslsetup.fetch_server_cert(MAX_HOST)
    if der is None:
        print(f"❌ Не удалось даже подключиться к {MAX_HOST}:443.")
        print("   Проверьте интернет, прокси и доступность домена.")
        return False

    issuer = sslsetup.describe_issuer(der)
    print(f"Сертификат сервера выдал: {issuer}")

    files = sslsetup.local_cert_files()
    print(f"Сертификатов в папке certs/: {len(files)}")
    for f in files:
        print(f"   • {os.path.basename(f)}")

    # Как видит ситуацию httpx по умолчанию (только certifi)
    import ssl as _ssl
    try:
        import certifi
        plain = _ssl.create_default_context(cafile=certifi.where())
        ok_certifi, _ = sslsetup.verify_host(MAX_HOST, ctx=plain)
        print(f"{'✅' if ok_certifi else '❌'} Проверка только по certifi "
              f"(так httpx работает по умолчанию)")
    except Exception:
        pass

    # Как будет видеть мост
    ctx = sslsetup.build_ssl_context(os.getenv("MAX_CA_BUNDLE") or None)
    ok, detail = sslsetup.verify_host(MAX_HOST, ctx=ctx)
    print(f"{'✅' if ok else '❌'} Проверка так, как её делает мост: {detail}")
    if not ok:
        print("\n   Не хватает корневого сертификата. Выполните:")
        print("       python install_ru_certs.py")
    return ok


ADVICE = """\
Соединения к localhost на этой машине перехватываются. Python (с версий
3.11.10 / 3.12.5 / 3.13) считает это попыткой подмены и отказывается работать.
Открытый баг CPython #129676.

┌── Если стоит Proxifier ──────────────────────────────────────────────┐
│ Выключать его целиком не нужно — достаточно исключить Python:        │
│                                                                      │
│   1. Profile -> Proxification Rules -> Add                           │
│   2. Name: Python                                                    │
│   3. Applications: python.exe; pythonw.exe                           │
│      (укажите полный путь к своему интерпретатору, например          │
│       c:\\ProgramData\\anaconda3\\python.exe)                           │
│   4. Action: Direct                                                  │
│   5. Поднимите правило ВЫШЕ остальных и сохраните                    │
│                                                                      │
│ Proxifier продолжит работать со всем остальным трафиком.             │
└──────────────────────────────────────────────────────────────────────┘

Другие возможные виновники: Fiddler, Charles, Burp Suite, VPN-клиенты,
сетевые экраны антивирусов (Kaspersky, ESET, Avast, Comodo), корпоративные
DLP-агенты. Для них — добавить python.exe в исключения.

Если сетевые фильтры повреждены, помогает сброс (нужны права администратора
и перезагрузка):

    netsh winsock reset

Проверьте также файл hosts на странные записи для localhost:

    C:\\Windows\\System32\\drivers\\etc\\hosts
    должно быть   127.0.0.1  localhost"""


def main() -> None:
    print("Диагностика окружения для моста MAX <-> Telegram")
    check_python()
    native_ok = check_socketpair()
    strategies_ok = check_strategies()
    asyncio_ok = check_asyncio()
    tls_ok = check_tls()

    line("ИТОГ")
    if not tls_ok:
        print("❌ Не проходит проверка TLS-сертификата MAX.")
        print("   MAX работает на сертификатах НУЦ Минцифры, которых нет ни в")
        print("   Windows, ни в наборе certifi. Решение:\n")
        print("       python install_ru_certs.py\n")
        print("   Либо скачайте сертификаты вручную с https://www.gosuslugi.ru/crt")
        print("   и положите в папку certs/ рядом с проектом.")
        if not asyncio_ok:
            print()
        else:
            return
    if native_ok and asyncio_ok and tls_ok:
        print("✅ Окружение исправно. Если main.py всё равно падает — дело")
        print("   в .env или токенах, покажите текст ошибки.")
        return
    if asyncio_ok and tls_ok:
        print("✅ Мост запустится: winfix подобрал рабочий способ и включается сам.")
        print("   Ничего дополнительно делать не нужно, можно запускать main.py.\n")
        print("   Если хотите убрать причину полностью, а не обходить её:\n")
        print(ADVICE)
        return
    if not strategies_ok:
        print("❌ Ни один способ не сработал — обойти программно не получится.\n")
    print(ADVICE)


if __name__ == "__main__":
    main()
