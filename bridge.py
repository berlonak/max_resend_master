# copyright by berlonak
# telegram: @Kilax123
"""Ядро пересылки.

Путь сообщения:
  приём -> дедуп -> анти-петля -> разрешение медиа -> скачивание
        -> архив -> отправка на другую платформу (с лимитами) -> mapping.
"""
from __future__ import annotations

import logging
import os

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    ReplyParameters,
)

from models import IncomingMessage
from utils import (
    MAX_TEXT_LIMIT,
    TG_CAPTION_LIMIT,
    TG_TEXT_LIMIT,
    MediaStore,
    compose_text,
    guess_max_upload_type,
    human_size,
    split_text,
)

log = logging.getLogger("bridge")

# Лимиты MAX на одно сообщение
MAX_ATTACHMENTS_PER_MESSAGE = 12
TG_MEDIA_GROUP_LIMIT = 10

# Лимиты размеров MAX по типам (байты)
MAX_SIZE_LIMITS = {
    "image": 50 * 1024 * 1024,
    "video": 250 * 1024 * 1024,
    "audio": 256 * 1024 * 1024,
    "file": 4 * 1024 * 1024 * 1024,
}


class Bridge:
    def __init__(self, cfg, db, max_client, tg_bot, media: MediaStore, limiter):
        self.cfg = cfg
        self.db = db
        self.max = max_client
        self.tg = tg_bot
        self.media = media
        self.limiter = limiter
        self.tg_bot_id: int | None = None

    # ================= точки входа =================
    async def from_max(self, msg: IncomingMessage) -> None:
        await self._relay(msg, target="tg")

    async def from_tg(self, msg: IncomingMessage) -> None:
        await self._relay(msg, target="max")

    async def _relay(self, msg: IncomingMessage, target: str) -> None:
        if await self._is_echo(msg):
            log.debug("Пропуск эха: %s/%s", msg.platform, msg.message_id)
            return
        if not msg.text and not msg.media:
            return
        await self._resolve_media(msg)
        await self._download(msg)
        await self.db.save_message(msg)
        if target == "tg":
            await self._send_to_tg(msg)
        else:
            await self._send_to_max(msg)

    # ================= защита от петли =================
    async def _is_echo(self, msg: IncomingMessage) -> bool:
        if await self.db.is_bridged_copy(msg.platform, msg.message_id):
            return True
        if msg.platform == "tg" and self.tg_bot_id and str(msg.sender_id) == str(self.tg_bot_id):
            return True
        if msg.platform == "max" and self.max.bot_id and str(msg.sender_id) == str(self.max.bot_id):
            return True
        return False

    # ================= подготовка медиа =================
    async def _resolve_media(self, msg: IncomingMessage) -> None:
        """Видео MAX часто приходит только токеном — достаём реальный url."""
        for m in msg.media:
            if m.file_ref or not m.video_token:
                continue
            try:
                info = await self.max.get_video(m.video_token)
                urls = info.get("urls") or {}
                m.file_ref = (urls.get("mp4_1080") or urls.get("mp4_720")
                              or urls.get("mp4_480") or urls.get("mp4_360")
                              or next(iter(urls.values()), ""))
                if not m.file_name:
                    m.file_name = f"video_{m.video_token[:12]}.mp4"
            except Exception as e:
                log.warning("Не удалось получить url видео MAX: %s", e)

    async def _download(self, msg: IncomingMessage) -> None:
        for m in msg.media:
            if m.local_path or not m.file_ref:
                continue
            try:
                if msg.platform == "max":
                    m.local_path = await self.media.download_url(m.file_ref, m.file_name)
                else:
                    f = await self.tg.get_file(m.file_ref)
                    # Облачный Bot API не отдаёт файлы больше 20 МБ.
                    if (f.file_size or 0) > self.cfg.tg_download_limit:
                        log.warning("Файл %s больше лимита скачивания Telegram (%s)",
                                    m.file_name, human_size(f.file_size or 0))
                        continue
                    dest = self.media.unique_path(m.file_name or f"{m.kind}_{m.file_ref}.bin")
                    await self.tg.download_file(f.file_path, destination=dest)
                    m.local_path = dest
                if m.local_path and not m.size:
                    m.size = os.path.getsize(m.local_path)
            except Exception as e:
                log.warning("Не удалось скачать медиа (%s): %s", m.kind, e)

    # ================= отправка в Telegram =================
    async def _send_to_tg(self, msg: IncomingMessage) -> None:
        chat = self.cfg.tg_chat_id
        key = f"tg:{chat}"
        reply_to = await self.db.dst_message_for(msg.platform,
                                                 msg.reply_to_source_id or "", "tg")
        rp = ReplyParameters(message_id=int(reply_to)) if reply_to else None
        tz = self.cfg.timezone
        part = 0

        try:
            usable = [m for m in msg.media if m.local_path]
            skipped = len(msg.media) - len(usable)

            # --- только текст ---
            if not usable:
                text = compose_text(msg, None, tz)
                if skipped:
                    text += f"\n\n⚠️ Вложений не передано: {skipped}"
                for chunk in split_text(text, TG_TEXT_LIMIT):
                    sent = await self._tg_call(key, lambda c=chunk, r=rp:
                                               self.tg.send_message(chat, c, reply_parameters=r))
                    await self._map(msg, "tg", sent.message_id, part)
                    part += 1
                    rp = None
                return

            # --- альбом ---
            if len(usable) > 1:
                await self._send_tg_album(msg, usable, rp, skipped)
                return

            # --- одиночное медиа ---
            m = usable[0]
            caption = compose_text(msg, m.caption, tz)
            head, tail = _split_caption(caption)
            file = FSInputFile(m.local_path)

            if m.kind in ("photo",) or (m.kind == "sticker" and not m.is_animated):
                call = lambda: self.tg.send_photo(chat, file, caption=head, reply_parameters=rp)
            elif m.kind == "animation":
                call = lambda: self.tg.send_animation(chat, file, caption=head, reply_parameters=rp)
            elif m.kind == "video":
                call = lambda: self.tg.send_video(chat, file, caption=head, reply_parameters=rp)
            elif m.kind == "voice":
                call = lambda: self.tg.send_voice(chat, file, caption=head, reply_parameters=rp)
            elif m.kind == "audio":
                call = lambda: self.tg.send_audio(chat, file, caption=head, reply_parameters=rp)
            else:
                call = lambda: self.tg.send_document(chat, file, caption=head, reply_parameters=rp)

            sent = await self._tg_call(key, call)
            await self._map(msg, "tg", sent.message_id, part)
            part += 1

            # хвост длинной подписи — отдельными сообщениями
            for chunk in tail:
                s = await self._tg_call(key, lambda c=chunk: self.tg.send_message(chat, c))
                await self._map(msg, "tg", s.message_id, part)
                part += 1
        except Exception as e:
            if "chat not found" in str(e).lower():
                log.error("Telegram не нашёл чат %s. Скорее всего неверный формат ID: "
                          "у обычной группы он выглядит как -1234567890, а префикс -100 "
                          "бывает только у супергрупп и каналов. Проверьте: python tg_check.py",
                          self.cfg.tg_chat_id)
            elif "bot was kicked" in str(e).lower() or "not a member" in str(e).lower():
                log.error("Бот удалён из чата Telegram %s — добавьте его обратно.",
                          self.cfg.tg_chat_id)
            else:
                log.exception("Ошибка отправки в Telegram: %s", e)

    async def _send_tg_album(self, msg, usable, rp, skipped: int) -> None:
        """Telegram принимает до 10 элементов в группе — режем по 10."""
        chat = self.cfg.tg_chat_id
        key = f"tg:{chat}"
        caption = compose_text(msg, None, self.cfg.timezone)
        head, tail = _split_caption(caption)
        part = 0

        for batch_no, start in enumerate(range(0, len(usable), TG_MEDIA_GROUP_LIMIT)):
            batch = usable[start:start + TG_MEDIA_GROUP_LIMIT]
            group = []
            for i, m in enumerate(batch):
                cap = head if (batch_no == 0 and i == 0) else None
                file = FSInputFile(m.local_path)
                if m.kind in ("photo", "sticker"):
                    group.append(InputMediaPhoto(media=file, caption=cap))
                elif m.kind in ("video", "animation"):
                    group.append(InputMediaVideo(media=file, caption=cap))
                else:
                    group.append(InputMediaDocument(media=file, caption=cap))
            if not group:
                continue
            sent = await self._tg_call(
                key, lambda g=group, r=(rp if batch_no == 0 else None):
                self.tg.send_media_group(chat, g, reply_parameters=r))
            for s in sent:
                await self._map(msg, "tg", s.message_id, part)
                part += 1

        for chunk in tail:
            s = await self._tg_call(key, lambda c=chunk: self.tg.send_message(chat, c))
            await self._map(msg, "tg", s.message_id, part)
            part += 1
        if skipped:
            await self._tg_call(key, lambda: self.tg.send_message(
                chat, f"⚠️ Вложений не передано: {skipped}"))

    async def _tg_call(self, key: str, factory):
        """Отправка с учётом лимитов и корректной обработкой 429."""
        async def run():
            try:
                return await factory()
            except TelegramRetryAfter as e:
                log.warning("Telegram 429, ждём %s c", e.retry_after)
                import asyncio
                await asyncio.sleep(e.retry_after + 1)
                return await factory()
        return await self.limiter.run(key, run)

    # ================= отправка в MAX =================
    async def _send_to_max(self, msg: IncomingMessage) -> None:
        chat = self.cfg.max_chat_id
        key = f"max:{chat}"
        reply_to = await self.db.dst_message_for(msg.platform,
                                                 msg.reply_to_source_id or "", "max")
        text = compose_text(msg, msg.media[0].caption if msg.media else None,
                            self.cfg.timezone)

        # Загружаем вложения, попутно отсеивая превышающие лимиты MAX.
        visual, files, skipped = [], [], 0
        for m in msg.media:
            if not m.local_path:
                skipped += 1
                continue
            up_type = guess_max_upload_type(m.kind)
            if m.size and m.size > MAX_SIZE_LIMITS.get(up_type, 0):
                log.warning("%s (%s) превышает лимит MAX для %s",
                            m.file_name, human_size(m.size), up_type)
                skipped += 1
                continue
            try:
                att = await self.max.upload(up_type, m.local_path)
            except Exception as e:
                log.warning("Загрузка в MAX не удалась (%s): %s", m.kind, e)
                skipped += 1
                continue
            # MAX не разрешает смешивать file с image/video в одном сообщении.
            (visual if up_type in ("image", "video") else files).append(att)

        if skipped:
            text += f"\n\n⚠️ Вложений не передано: {skipped}"

        try:
            part = 0
            groups = [g for g in (visual, files) if g] or [[]]
            first = True
            for group in groups:
                # до 12 вложений в одном сообщении
                for start in range(0, max(len(group), 1), MAX_ATTACHMENTS_PER_MESSAGE):
                    batch = group[start:start + MAX_ATTACHMENTS_PER_MESSAGE]
                    body = text if first else None
                    chunks = split_text(body, MAX_TEXT_LIMIT) if body else [None]

                    for ci, chunk in enumerate(chunks):
                        res = await self.limiter.run(key, lambda c=chunk, b=batch, ci=ci:
                            self.max.send_with_retry(
                                chat,
                                text=c,
                                attachments=b if ci == 0 and b else None,
                                reply_to_mid=reply_to if first and ci == 0 else None,
                            ))
                        mid = _extract_max_mid(res)
                        if mid:
                            await self._map(msg, "max", mid, part)
                            part += 1
                    first = False
        except Exception as e:
            log.exception("Ошибка отправки в MAX: %s", e)

    # ================= вспомогательное =================
    async def _map(self, msg, dst_platform: str, dst_message_id, part: int = 0) -> None:
        await self.db.map_forward(msg.platform, msg.message_id,
                                  dst_platform, dst_message_id, part)


def _split_caption(caption: str) -> tuple[str, list[str]]:
    """Подпись Telegram ограничена 1024 символами — остальное отдельными сообщениями."""
    if len(caption) <= TG_CAPTION_LIMIT:
        return caption, []
    parts = split_text(caption, TG_CAPTION_LIMIT)
    return parts[0], parts[1:]


def _extract_max_mid(res) -> str | None:
    if not isinstance(res, dict):
        return None
    try:
        return res["message"]["body"]["mid"]
    except (KeyError, TypeError):
        return res.get("mid")
