# copyright by berlonak
# telegram: @Kilax123
"""Ограничение частоты отправки.

Подтверждённые лимиты:
  MAX      — до 30 rps суммарно и не более 2 сообщений/сек в один чат;
  Telegram — точных чисел не гарантирует, отвечает 429 с retry_after.

Поэтому: глобальный лимитер + отдельный лимитер на каждый чат,
плюс последовательность внутри чата (concurrency = 1), чтобы не ломать порядок.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    """Token bucket: не более `rate` операций за `per` секунд."""

    def __init__(self, rate: float, per: float = 1.0):
        self.rate = rate
        self.per = per
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.rate,
                                   self._tokens + (now - self._last) * self.rate / self.per)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) * self.per / self.rate)


class ChatLimiter:
    """Пер-чатный лимит + глобальный потолок + строгий порядок внутри чата."""

    def __init__(self, per_chat_rate: float = 2.0, global_rate: float = 25.0):
        self._per_chat_rate = per_chat_rate
        self._global = RateLimiter(global_rate)
        self._chat: dict[str, RateLimiter] = {}
        self._order: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _bucket(self, chat_key: str) -> RateLimiter:
        if chat_key not in self._chat:
            self._chat[chat_key] = RateLimiter(self._per_chat_rate)
        return self._chat[chat_key]

    async def run(self, chat_key: str, coro_factory):
        """Выполняет отправку с соблюдением лимитов и порядка.

        coro_factory — функция без аргументов, возвращающая корутину.
        """
        async with self._order[chat_key]:          # порядок внутри чата
            await self._bucket(chat_key).acquire()  # лимит чата
            await self._global.acquire()            # общий лимит
            return await coro_factory()
