# copyright by berlonak
# telegram: @Kilax123
"""Обход бага socketpair на Windows (CPython #129676) — версия 2.

Проблема
--------
На Windows нет системного socketpair(), поэтому asyncio эмулирует его
TCP-соединением на localhost. Начиная с исправления PSF-2024-7 Python
проверяет, что соединение действительно своё, сравнивая адреса сторон:

    ssock.getsockname() != csock.getpeername()  ->  ConnectionError

Perехватчики локального трафика (Proxifier, VPN, сетевые экраны антивирусов)
подменяют адреса, и проверка срабатывает ложно. asyncio использует socketpair
для внутреннего "self-pipe", поэтому падает вообще любая асинхронная программа.

Решение
-------
Пробуем несколько способов и берём первый рабочий:

  1. Стандартный socketpair — вдруг всё исправно.
  2. IPv6 (::1) со штатной проверкой адресов — многие перехватчики
     работают только с IPv4 и localhost по IPv6 не трогают.
  3. IPv4 с подтверждением через секретный токен вместо сравнения адресов.
  4. IPv6 с подтверждением через секретный токен.

Проверка безопасности НЕ отключается. Там, где сравнение адресов не работает,
стороны обмениваются случайным 32-байтовым секретом: посторонний процесс его
не угадает, а честный перехватчик просто передаст байты дальше.
"""
from __future__ import annotations

import os
import socket as _sock

_NONCE_SIZE = 32
_TIMEOUT = 10.0
_ACCEPT_ATTEMPTS = 5

_original_socketpair = _sock.socketpair
_applied = False
_strategy_name: str | None = None


# ----------------------------------------------------------------------
# вспомогательное
# ----------------------------------------------------------------------
def _recv_exactly(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _host_for(family) -> str:
    return "127.0.0.1" if family == _sock.AF_INET else "::1"


# ----------------------------------------------------------------------
# способ 1: сравнение адресов (как в стандартной библиотеке), но с выбором семейства
# ----------------------------------------------------------------------
def _pair_by_address(family):
    host = _host_for(family)
    lsock = _sock.socket(family, _sock.SOCK_STREAM)
    try:
        lsock.bind((host, 0))
        lsock.listen(2)
        lsock.settimeout(_TIMEOUT)
        addr, port = lsock.getsockname()[:2]

        csock = _sock.socket(family, _sock.SOCK_STREAM)
        try:
            # Блокирующий connect с таймаутом: после его возврата сокет
            # гарантированно подключён. Неблокирующий вариант из стандартной
            # библиотеки этого не гарантирует — отсюда WinError 10057.
            csock.settimeout(_TIMEOUT)
            csock.connect((addr, port))
            ssock, _peer = lsock.accept()
            try:
                if (ssock.getsockname() != csock.getpeername()
                        or csock.getsockname() != ssock.getpeername()):
                    ssock.close()
                    raise ConnectionError("Unexpected peer connection")
            except BaseException:
                ssock.close()
                raise
            ssock.settimeout(None)
            csock.settimeout(None)
            return ssock, csock
        except BaseException:
            csock.close()
            raise
    finally:
        lsock.close()


# ----------------------------------------------------------------------
# способ 2: подтверждение секретным токеном
# ----------------------------------------------------------------------
def _pair_by_nonce(family):
    host = _host_for(family)
    lsock = _sock.socket(family, _sock.SOCK_STREAM)
    try:
        lsock.bind((host, 0))
        lsock.listen(_ACCEPT_ATTEMPTS + 1)
        lsock.settimeout(_TIMEOUT)
        addr, port = lsock.getsockname()[:2]

        csock = _sock.socket(family, _sock.SOCK_STREAM)
        try:
            csock.settimeout(_TIMEOUT)
            csock.connect((addr, port))     # блокирующий -> сокет точно подключён

            nonce = os.urandom(_NONCE_SIZE)
            csock.sendall(nonce)

            # Если в порт влез посторонний, отбрасываем его и слушаем дальше:
            # наше собственное соединение всё ещё ждёт в очереди.
            ssock = None
            for _ in range(_ACCEPT_ATTEMPTS):
                candidate, _peer = lsock.accept()
                candidate.settimeout(_TIMEOUT)
                try:
                    received = _recv_exactly(candidate, _NONCE_SIZE)
                except OSError:
                    received = b""
                if received == nonce:
                    candidate.settimeout(None)
                    ssock = candidate
                    break
                candidate.close()

            if ssock is None:
                raise ConnectionError(
                    "socketpair: подтверждение не прошло — соединение к localhost "
                    "перехватывается или перенаправляется")

            csock.settimeout(None)
            return ssock, csock
        except BaseException:
            csock.close()
            raise
    finally:
        lsock.close()


# ----------------------------------------------------------------------
# перебор способов
# ----------------------------------------------------------------------
def _has_ipv6() -> bool:
    if not _sock.has_ipv6:
        return False
    try:
        s = _sock.socket(_sock.AF_INET6, _sock.SOCK_STREAM)
        s.bind(("::1", 0))
        s.close()
        return True
    except OSError:
        return False


def _strategies():
    out = [
        ("стандартный socketpair", lambda: _original_socketpair()),
        ("IPv4 + сравнение адресов", lambda: _pair_by_address(_sock.AF_INET)),
    ]
    if _has_ipv6():
        out.append(("IPv6 (::1) + сравнение адресов",
                    lambda: _pair_by_address(_sock.AF_INET6)))
    out.append(("IPv4 + секретный токен", lambda: _pair_by_nonce(_sock.AF_INET)))
    if _has_ipv6():
        out.append(("IPv6 (::1) + секретный токен",
                    lambda: _pair_by_nonce(_sock.AF_INET6)))
    return out


def _works(factory) -> bool:
    """Проверяет способ по-настоящему: создаёт пару и гоняет через неё данные."""
    try:
        a, b = factory()
    except Exception:
        return False
    try:
        a.settimeout(_TIMEOUT)
        b.settimeout(_TIMEOUT)
        a.sendall(b"\x01ping")
        if b.recv(5) != b"\x01ping":
            return False
        b.sendall(b"\x02pong")
        if a.recv(5) != b"\x02pong":
            return False
        return True
    except Exception:
        return False
    finally:
        try:
            a.close()
            b.close()
        except Exception:
            pass


def probe() -> list[tuple[str, bool]]:
    """Возвращает список (название способа, работает ли). Для диагностики."""
    return [(name, _works(factory)) for name, factory in _strategies()]


def apply(verbose: bool = True) -> bool:
    """Подбирает рабочий способ и подменяет socket.socketpair при необходимости.

    Возвращает True, если патч был применён.
    """
    global _applied, _strategy_name

    if _applied:
        return True

    strategies = _strategies()

    # Если штатный способ работает — ничего не трогаем.
    if _works(strategies[0][1]):
        return False

    for name, factory in strategies[1:]:
        if not _works(factory):
            continue

        def patched(family=None, type=_sock.SOCK_STREAM, proto=0, _f=factory):
            # Нестандартные параметры отдаём оригинальной реализации.
            if type != _sock.SOCK_STREAM or proto != 0:
                return _original_socketpair(family or _sock.AF_INET, type, proto)
            return _f()

        _sock.socketpair = patched
        _applied = True
        _strategy_name = name
        if verbose:
            print(f"[winfix] Локальные соединения перехватываются. "
                  f"Рабочий способ: {name}.")
        return True

    if verbose:
        print("[winfix] ВНИМАНИЕ: ни один способ создать socketpair не сработал.\n"
              "         Запустите  python diagnose.py  для подробностей.")
    return False


def strategy() -> str | None:
    return _strategy_name
