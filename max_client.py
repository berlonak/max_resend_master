# copyright by berlonak
# telegram: @Kilax123
"""Клиент MAX Bot API.

Приведён в соответствие со спецификацией (проверка документации 03.08.2026):
  - базовый домен platform-api2.max.ru (обязателен с 19.07.2026);
  - авторизация ТОЛЬКО заголовком, голым токеном без Bearer;
  - upload: токен может прийти как при получении URL (video/audio),
    так и при заливке файла (image/file) — читаем оба;
  - видео во входящих может быть только токеном -> GET /videos/{token};
  - разделение ошибок на повторяемые и окончательные.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

import httpx

log = logging.getLogger("max")

DEFAULT_BASE = "https://platform-api2.max.ru"


class MaxError(Exception):
    """Ошибка MAX API с признаком «можно ли повторить»."""

    def __init__(self, message: str, status: int = 0, body=None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


class MaxClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE, verify=True):
        self.token = token
        self.base = base_url.rstrip("/")
        # Авторизация — голым токеном в заголовке. Не Bearer, не query string.
        # verify — контекст TLS: MAX использует сертификаты НУЦ Минцифры,
        # которых нет ни в системе, ни в certifi (см. sslsetup.py).
        self._http = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"Authorization": token},
            verify=verify,
        )
        self.bot_id: str | None = None

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # базовый запрос с разбором ошибок
    # ------------------------------------------------------------------
    async def _request(self, method: str, path: str, *, params=None,
                       json=None, timeout: float | None = None):
        try:
            r = await self._http.request(method, f"{self.base}{path}",
                                         params=params, json=json, timeout=timeout)
        except httpx.ConnectError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise MaxError(
                    "Не удалось проверить TLS-сертификат MAX. Скорее всего не хватает "
                    "корневого сертификата НУЦ Минцифры. Запустите: python install_ru_certs.py "
                    "(подробности: python diagnose.py)",
                    retryable=False) from e
            raise MaxError(f"сетевая ошибка: {e}", retryable=True) from e
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise MaxError(f"сетевая ошибка: {e}", retryable=True) from e

        ctype = r.headers.get("content-type", "")
        body = r.json() if "application/json" in ctype and r.content else r.text

        if r.is_success:
            return body

        raise MaxError(f"MAX API {r.status_code}: {body}", r.status_code, body,
                       retryable=_is_retryable(r.status_code, body))

    # ------------------------------------------------------------------
    # методы API
    # ------------------------------------------------------------------
    async def get_me(self) -> dict:
        data = await self._request("GET", "/me")
        self.bot_id = str(data.get("user_id") or data.get("id") or "")
        return data

    async def get_chat(self, chat_id: int) -> dict:
        return await self._request("GET", f"/chats/{chat_id}")

    async def get_my_membership(self, chat_id: int) -> dict:
        """Права бота в чате. Нужен read_all_messages, иначе события не придут."""
        return await self._request("GET", f"/chats/{chat_id}/members/me")

    async def get_updates(self, marker: int | None, timeout: int = 30, limit: int = 100) -> dict:
        """Long polling. Только для разработки — в production используйте webhook."""
        params: dict = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params, timeout=timeout + 15)

    async def create_subscription(self, url: str, update_types: list[str], secret: str) -> dict:
        return await self._request("POST", "/subscriptions", json={
            "url": url, "update_types": update_types, "secret": secret,
        })

    async def get_subscriptions(self) -> dict:
        return await self._request("GET", "/subscriptions")

    async def send_message(self, chat_id: int, text: str | None = None,
                           attachments: list[dict] | None = None,
                           reply_to_mid: str | None = None,
                           fmt: str | None = None,
                           notify: bool = True) -> dict:
        body: dict = {"notify": notify}
        if text:
            body["text"] = text
        if fmt:
            body["format"] = fmt              # markdown | html
        if attachments:
            body["attachments"] = attachments
        if reply_to_mid:
            body["link"] = {"type": "reply", "mid": reply_to_mid}
        return await self._request("POST", "/messages",
                                   params={"chat_id": chat_id}, json=body)

    async def edit_message(self, message_id: str, text: str | None = None,
                           attachments: list[dict] | None = None,
                           fmt: str | None = None) -> dict:
        body: dict = {}
        if text is not None:
            body["text"] = text
        if fmt:
            body["format"] = fmt
        if attachments:
            body["attachments"] = attachments
        return await self._request("PUT", "/messages",
                                   params={"message_id": message_id}, json=body)

    async def delete_message(self, message_id: str) -> dict:
        return await self._request("DELETE", "/messages",
                                   params={"message_id": message_id})

    async def get_messages(self, chat_id: int, count: int = 10) -> dict:
        """Последние сообщения чата. Заодно проверка, что бот их вообще видит."""
        return await self._request("GET", "/messages",
                                   params={"chat_id": chat_id, "count": count})

    async def get_chats(self, count: int = 50) -> dict:
        """С июня 2026 список чатов больше не отдаётся — оставлено для диагностики."""
        return await self._request("GET", "/chats", params={"count": count})

    async def delete_subscription(self, url: str) -> dict:
        return await self._request("DELETE", "/subscriptions", params={"url": url})

    async def get_video(self, video_token: str) -> dict:
        """Входящее видео часто содержит только токен — здесь получаем реальные urls."""
        return await self._request("GET", f"/videos/{video_token}")

    # ------------------------------------------------------------------
    # загрузка файлов
    # ------------------------------------------------------------------
    async def upload(self, kind: str, local_path: str) -> dict:
        """Загружает файл и возвращает готовый объект attachment.

        Токен приходит по-разному в зависимости от типа:
          - video/audio: обычно уже в ответе на POST /uploads;
          - image/file:  обычно в ответе самой заливки.
        Поэтому читаем оба ответа и берём то, что нашлось.
        """
        slot = await self._request("POST", "/uploads", params={"type": kind})
        upload_url = slot.get("url")
        if not upload_url:
            raise MaxError("POST /uploads не вернул url", body=slot)

        # Заливка идёт на выданный URL «как есть», без нашего заголовка авторизации.
        with open(local_path, "rb") as f:
            files = {"data": (os.path.basename(local_path), f)}
            try:
                r = await self._http.post(upload_url, files=files,
                                          headers={"Authorization": ""}, timeout=600)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise MaxError(f"заливка файла не удалась: {e}", retryable=True) from e

        if not r.is_success:
            raise MaxError(f"заливка файла: HTTP {r.status_code}", r.status_code,
                           retryable=r.status_code >= 500)

        uploaded = {}
        if r.content:
            try:
                uploaded = r.json()
            except Exception:
                uploaded = {}

        # image может вернуть готовый набор photos — передаём как есть
        if kind == "image" and isinstance(uploaded.get("photos"), (dict, list)):
            return {"type": "image", "payload": {"photos": uploaded["photos"]}}

        token = uploaded.get("token") or slot.get("token")
        if not token:
            raise MaxError(f"загрузка прошла, но токен не найден (тип {kind})",
                           body={"slot": slot, "uploaded": uploaded})
        return {"type": kind, "payload": {"token": token}}

    async def send_with_retry(self, chat_id: int, *, attempts: int = 5, **kwargs) -> dict:
        """Отправка с повтором: свежезалитое медиа может быть ещё не готово."""
        last: Exception | None = None
        for i in range(attempts):
            try:
                return await self.send_message(chat_id, **kwargs)
            except MaxError as e:
                last = e
                if not e.retryable:
                    raise
                delay = min(60, 2 ** i) + random.random()
                log.warning("MAX send повтор через %.1fs (%s)", delay, e)
                await asyncio.sleep(delay)
        raise last  # type: ignore[misc]


def _is_retryable(status: int, body) -> bool:
    """429, 5xx и «медиа ещё обрабатывается» — повторяем. 400/401/403 — нет."""
    if status == 429 or status >= 500:
        return True
    text = str(body).lower()
    if "attachment.not.ready" in text or "not.ready" in text:
        return True
    return False
