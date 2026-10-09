# copyright by berlonak
# telegram: @Kilax123
"""Точка входа: слушатели Telegram и MAX + общее ядро."""
from __future__ import annotations

# Обход бага socketpair на Windows. Должно идти ДО создания цикла событий,
# иначе asyncio упадёт на ConnectionError("Unexpected peer connection").
import winfix

winfix.apply()

import asyncio
import logging
import os
import time
from collections import defaultdict

from aiogram import Bot as TgBot
from aiogram import Dispatcher, F
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message

from bridge import Bridge
from config import load_config
from db import Db
from max_client import MaxClient
from models import IncomingMessage, MediaItem
from ratelimit import ChatLimiter
from sslsetup import build_ssl_context
from utils import MediaStore

logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


# ======================= Telegram =======================
def tg_to_incoming(m: Message) -> IncomingMessage:
    """Автор по приоритету: from -> sender_chat -> author_signature -> chat.title."""
    is_channel = m.chat.type == "channel" or m.sender_chat is not None
    if m.from_user:
        sender_id = str(m.from_user.id)
        sender_name = m.from_user.full_name
        username = m.from_user.username
    elif m.sender_chat:
        sender_id = str(m.sender_chat.id)
        sender_name = m.sender_chat.title or "Канал"
        username = m.sender_chat.username
    else:
        sender_id = str(m.chat.id)
        sender_name = m.chat.title or "Автор не раскрыт платформой"
        username = None

    cap = m.caption
    media: list[MediaItem] = []
    if m.photo:
        p = m.photo[-1]                      # наибольший размер
        media.append(MediaItem("photo", p.file_id, caption=cap, size=p.file_size or 0))
    if m.video:
        media.append(MediaItem("video", m.video.file_id, file_name=m.video.file_name,
                               mime=m.video.mime_type, caption=cap,
                               size=m.video.file_size or 0))
    if m.animation:
        a = m.animation
        media.append(MediaItem("animation", a.file_id, file_name=a.file_name,
                               mime=a.mime_type, caption=cap, size=a.file_size or 0))
    if m.voice:
        media.append(MediaItem("voice", m.voice.file_id, file_name="voice.ogg",
                               mime=m.voice.mime_type, caption=cap,
                               size=m.voice.file_size or 0))
    if m.audio:
        media.append(MediaItem("audio", m.audio.file_id, file_name=m.audio.file_name,
                               mime=m.audio.mime_type, caption=cap,
                               size=m.audio.file_size or 0))
    if m.document:
        media.append(MediaItem("document", m.document.file_id,
                               file_name=m.document.file_name,
                               mime=m.document.mime_type, caption=cap,
                               size=m.document.file_size or 0))
    if m.sticker:
        s = m.sticker
        media.append(MediaItem("sticker", s.file_id,
                               file_name=f"sticker_{s.file_unique_id}.webp",
                               is_animated=bool(s.is_animated or s.is_video),
                               caption=(s.emoji or ""), size=s.file_size or 0))

    return IncomingMessage(
        platform="tg",
        chat_id=str(m.chat.id),
        message_id=str(m.message_id),
        sender_id=sender_id,
        sender_name=sender_name,
        sender_username=username,
        text=m.text or "",
        timestamp=int(m.date.timestamp()),
        media=media,
        album_id=m.media_group_id,
        reply_to_source_id=str(m.reply_to_message.message_id) if m.reply_to_message else None,
        is_channel_post=is_channel,
        author_signature=m.author_signature,
    )


class AlbumBuffer:
    """Собирает части Telegram-альбома (общий media_group_id) в одно сообщение."""

    def __init__(self, bridge: Bridge, debounce: float):
        self.bridge = bridge
        self.debounce = debounce
        self._buf: dict[str, list[Message]] = defaultdict(list)
        self._tasks: dict[str, asyncio.Task] = {}

    def add(self, m: Message) -> None:
        gid = m.media_group_id
        self._buf[gid].append(m)
        if gid in self._tasks:
            self._tasks[gid].cancel()
        self._tasks[gid] = asyncio.create_task(self._flush_later(gid))

    async def _flush_later(self, gid: str) -> None:
        try:
            await asyncio.sleep(self.debounce)
        except asyncio.CancelledError:
            return
        msgs = sorted(self._buf.pop(gid, []), key=lambda x: x.message_id)
        self._tasks.pop(gid, None)
        if not msgs:
            return
        base = tg_to_incoming(msgs[0])
        base.media = []
        for mm in msgs:
            base.media.extend(tg_to_incoming(mm).media)
            if not base.text:
                base.text = mm.caption or ""
        await self.bridge.from_tg(base)


async def run_telegram(bridge: Bridge, album: AlbumBuffer, cfg, db: Db) -> None:
    tg = bridge.tg
    dp = Dispatcher()

    async def handle(m: Message) -> None:
        if m.chat.id != cfg.tg_chat_id:
            return
        # Идемпотентность: повторная доставка не создаёт дубль.
        if await db.seen_update(f"tg:{m.chat.id}:{m.message_id}"):
            return
        if m.media_group_id:
            album.add(m)
        else:
            await bridge.from_tg(tg_to_incoming(m))

    dp.message.register(handle)
    dp.channel_post.register(handle)

    me = await tg.get_me()
    bridge.tg_bot_id = me.id
    log.info("Telegram: @%s (id=%s)", me.username, me.id)

    # Проверяем чат сразу — иначе ошибка вылезет только на первом сообщении.
    try:
        chat = await tg.get_chat(cfg.tg_chat_id)
        log.info("Telegram чат: «%s» (%s)", chat.title or chat.full_name, chat.type)
        if chat.type == "group" and str(cfg.tg_chat_id).lstrip("-").startswith("100"):
            log.warning("Похоже, к ID обычной группы добавлен префикс -100. "
                        "Если пересылка не пойдёт — запустите python tg_check.py")
    except Exception as e:
        log.error("НЕ УДАЛОСЬ ОТКРЫТЬ ЧАТ TELEGRAM TG_CHAT_ID=%s: %s", cfg.tg_chat_id, e)
        log.error("Пересылка MAX -> Telegram работать не будет. "
                  "Частая причина — формат ID: у обычной группы он вида -1234567890, "
                  "префикс -100 только у супергрупп и каналов.")
        log.error("Запустите:  python tg_check.py  — он подберёт правильный ID.")

    if me.can_read_all_group_messages is False:
        log.warning("У бота Telegram включён Privacy Mode: в обычной группе он видит "
                    "только команды и ответы себе, обычные сообщения не приходят. "
                    "Отключите у @BotFather (/setprivacy -> Disable), затем удалите "
                    "бота из группы и добавьте заново.")
    await dp.start_polling(tg, handle_signals=False,
                           allowed_updates=["message", "channel_post"])


# ======================= MAX =======================
def _extract_chat_id(update: dict, msg: dict) -> str:
    """chat_id встречается в разных местах в зависимости от типа события."""
    recipient = msg.get("recipient") or {}
    for candidate in (recipient.get("chat_id"),
                      msg.get("chat_id"),
                      update.get("chat_id"),
                      (update.get("chat") or {}).get("chat_id"),
                      (update.get("chat") or {}).get("id")):
        if candidate not in (None, ""):
            return str(candidate)
    return ""


def max_update_to_incoming(update: dict) -> IncomingMessage | None:
    if update.get("update_type") != "message_created":
        return None
    msg = update.get("message", {}) or {}
    sender = msg.get("sender") or {}
    recipient = msg.get("recipient", {}) or {}
    body = msg.get("body", {}) or {}

    media: list[MediaItem] = []
    for att in body.get("attachments", []) or []:
        item = _max_attachment(att)
        if item:
            media.append(item)

    reply = None
    link = msg.get("link") or {}
    if link.get("type") == "reply":
        reply = str((link.get("message") or {}).get("mid") or link.get("mid") or "") or None

    # sender может отсутствовать (посты каналов) — не выдумываем пользователя
    if sender:
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) \
               or sender.get("name") or ""
        sender_id = str(sender.get("user_id", ""))
        username = sender.get("username")
        is_channel = False
    else:
        name = "Автор не раскрыт платформой"
        sender_id = ""
        username = None
        is_channel = True

    return IncomingMessage(
        platform="max",
        chat_id=_extract_chat_id(update, msg),
        message_id=str(body.get("mid", "")),
        sender_id=sender_id,
        sender_name=name,
        sender_username=username,
        text=body.get("text", "") or "",
        timestamp=int(msg.get("timestamp") or update.get("timestamp") or time.time()),
        media=media,
        reply_to_source_id=reply,
        is_channel_post=is_channel,
    )


def _max_attachment(att: dict) -> MediaItem | None:
    kmap = {"image": "photo", "photo": "photo", "video": "video", "audio": "audio",
            "voice": "voice", "file": "document", "sticker": "sticker"}
    atype = att.get("type")
    if atype in ("inline_keyboard", "keyboard", "contact", "location", "share"):
        return None                     # не медиа — обрабатывается отдельно
    kind = kmap.get(atype, "document")
    payload = att.get("payload", {}) or {}
    url = payload.get("url") or payload.get("file_url") or ""
    token = payload.get("token")
    # Видео может прийти без url — тогда url добираем через GET /videos/{token}
    if not url and atype == "video" and token:
        return MediaItem(kind, "", file_name=payload.get("filename"), video_token=token)
    if not url:
        return None
    return MediaItem(kind, url, file_name=payload.get("filename"), mime=payload.get("mime"))


async def run_max(bridge: Bridge, cfg, db: Db) -> None:
    client = bridge.max
    me = await client.get_me()
    log.info("MAX: %s (id=%s)", me.get("name") or me.get("first_name"), client.bot_id)

    # Активная webhook-подписка перехватывает события — long polling останется пустым.
    try:
        subs = await client.get_subscriptions()
        items = subs.get("subscriptions", subs) if isinstance(subs, dict) else subs
        if items:
            log.warning("В MAX есть активная webhook-подписка (%d шт). Пока она есть, "
                        "long polling может не получать события. Проверьте: python max_check.py",
                        len(items))
    except Exception as e:
        log.debug("Подписки проверить не удалось: %s", e)

    # Без read_all_messages бот сидит в чате, но событий не получает.
    try:
        member = await client.get_my_membership(cfg.max_chat_id)
        perms = member.get("permissions")
        if perms is None:
            log.warning("В ответе о правах нет поля permissions — бот, скорее всего, "
                        "НЕ администратор чата MAX. Без права read_all_messages сообщения "
                        "приходить не будут. Подробности: python max_check.py")
        elif "read_all_messages" not in perms:
            log.warning("У бота MAX НЕТ права read_all_messages — сообщения приходить "
                        "не будут. Текущие права: %s", perms)
        else:
            log.info("Права бота MAX в чате: %s", perms)
    except Exception as e:
        log.warning("Не удалось проверить права бота в чате MAX: %s", e)

    marker: int | None = None
    while True:
        try:
            data = await client.get_updates(marker, timeout=30)
            for upd in data.get("updates", []) or []:
                log.debug("MAX событие: %s", upd)
                inc = max_update_to_incoming(upd)
                if not inc:
                    log.info("MAX: событие %s пропущено (не новое сообщение)",
                             upd.get("update_type"))
                    continue
                if str(inc.chat_id) != str(cfg.max_chat_id):
                    log.warning("MAX: сообщение из чата %s не совпадает с MAX_CHAT_ID=%s "
                                "— пропущено. Если это ваш чат, поправьте .env",
                                inc.chat_id or "<не найден в событии>", cfg.max_chat_id)
                    continue
                key = f"max:{upd.get('update_type')}:{inc.chat_id}:{inc.message_id}:{inc.timestamp}"
                if await db.seen_update(key):
                    continue
                await bridge.from_max(inc)
            marker = data.get("marker", marker)
        except Exception as e:
            log.warning("MAX polling: %s", e)
            await asyncio.sleep(3)


async def housekeeping(db: Db) -> None:
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            await db.purge_old_updates()
        except Exception as e:
            log.warning("Очистка дедупа: %s", e)


# ======================= запуск =======================
async def main() -> None:
    cfg = load_config()
    db = Db(cfg.db_path)
    await db.open()
    # MAX работает на сертификатах НУЦ Минцифры — собираем доверие явно.
    verify = build_ssl_context(cfg.ca_bundle, cfg.ssl_verify)
    if verify is False:
        log.warning("ПРОВЕРКА TLS ОТКЛЮЧЕНА (SSL_VERIFY=false). "
                    "Так можно только при отладке — трафик уязвим к перехвату.")

    media = MediaStore(cfg.media_dir, verify=verify)
    max_client = MaxClient(cfg.max_token, cfg.max_base_url, verify=verify)

    session = None
    if cfg.tg_api_base.rstrip("/") != "https://api.telegram.org":
        session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.tg_api_base))
    tg_bot = TgBot(cfg.tg_token, session=session) if session else TgBot(cfg.tg_token)

    limiter = ChatLimiter(per_chat_rate=2.0, global_rate=25.0)
    bridge = Bridge(cfg, db, max_client, tg_bot, media, limiter)
    album = AlbumBuffer(bridge, cfg.album_debounce)

    log.info("Мост MAX <-> Telegram запускается (локальный Bot API: %s)", cfg.local_bot_api)
    try:
        await asyncio.gather(
            run_telegram(bridge, album, cfg, db),
            run_max(bridge, cfg, db),
            housekeeping(db),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Останавливаюсь…")
    finally:
        await media.close()
        await max_client.close()
        await tg_bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nМост остановлен.")
