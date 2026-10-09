# copyright by berlonak
# telegram: @Kilax123
"""Подписи, время, разбиение текста и работа с медиафайлами."""
from __future__ import annotations

import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

PLATFORM_LABEL = {"max": "MAX", "tg": "Telegram"}

# Лимиты текста: MAX — 4000, Telegram — 4096. Берём меньший как общий безопасный.
MAX_TEXT_LIMIT = 4000
TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024


def unix_to_dt(raw: int | str, tz: str = "Europe/Moscow") -> datetime:
    """Нормализует время.

    Telegram отдаёт секунды, MAX в части полей — миллисекунды.
    Порог отделяет одно от другого: значение больше 1e11 — это уже миллисекунды.
    """
    n = int(raw)
    ms = n if n > 100_000_000_000 else n * 1000
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(tz))


def format_header(msg, tz: str = "Europe/Moscow", as_html: bool = False) -> str:
    """Шапка «кто / откуда / когда». Имена экранируются — они пользовательский ввод."""
    esc = html.escape if as_html else (lambda s: s)

    if msg.is_channel_post:
        who = f"📣 {esc(msg.sender_name or 'Канал')}"
        if msg.author_signature:
            who += f"\n✍️ {esc(msg.author_signature)}"
    else:
        who = f"👤 {esc(msg.sender_name or 'Автор не раскрыт платформой')}"
        if msg.sender_username:
            who += f" (@{esc(msg.sender_username)})"

    when = unix_to_dt(msg.timestamp, tz).strftime("%d.%m.%Y %H:%M")
    src = PLATFORM_LABEL.get(msg.platform, msg.platform)
    return f"{who} · {src}\n🕒 {when}"


def compose_text(msg, original_caption: str | None = None,
                 tz: str = "Europe/Moscow", as_html: bool = False) -> str:
    esc = html.escape if as_html else (lambda s: s)
    parts = [format_header(msg, tz, as_html)]
    body = (msg.text or original_caption or "").strip()
    if body:
        parts.append("—" * 12)
        parts.append(esc(body))
    return "\n".join(parts)


def split_text(text: str, limit: int) -> list[str]:
    """Режет длинный текст по границам абзацев, затем строк, затем жёстко."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def guess_max_upload_type(kind: str) -> str:
    """Наш вид медиа -> тип загрузки MAX (image|video|audio|file)."""
    return {
        "photo": "image", "sticker": "image", "animation": "video",
        "video": "video", "audio": "audio", "voice": "audio",
    }.get(kind, "file")


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


class MediaStore:
    """Скачивает входящие файлы на диск и раздаёт локальные пути."""

    def __init__(self, media_dir: str, verify=True):
        self.dir = media_dir
        os.makedirs(media_dir, exist_ok=True)
        self._http = httpx.AsyncClient(timeout=600, follow_redirects=True, verify=verify)

    async def close(self) -> None:
        await self._http.aclose()

    async def download_url(self, url: str, hint_name: str | None = None) -> str:
        """Потоковое скачивание по прямой ссылке — файл не держим целиком в памяти."""
        path = self.unique_path(hint_name or _name_from_url(url))
        async with self._http.stream("GET", url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes(1024 * 256):
                    f.write(chunk)
        return path

    def unique_path(self, name: str) -> str:
        safe = _safe_name(name)
        base = os.path.join(self.dir, safe)
        root, ext = os.path.splitext(base)
        cand, i = base, 0
        while os.path.exists(cand):
            i += 1
            cand = f"{root}_{i}{ext}"
        return cand


def _safe_name(name: str) -> str:
    """Защита от путей вида ../../etc/passwd в присланном имени файла."""
    name = os.path.basename(name or "file.bin")
    name = name.replace("\x00", "").strip() or "file.bin"
    return name[:120]


def _name_from_url(url: str) -> str:
    tail = url.split("?")[0].rstrip("/").split("/")[-1]
    return tail or "file.bin"
