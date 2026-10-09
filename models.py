# copyright by berlonak
# telegram: @Kilax123
"""Платформо-независимое представление входящего сообщения."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MediaItem:
    kind: str                    # photo | video | audio | voice | document | sticker | animation
    file_ref: str = ""           # file_id (TG) либо прямой url (MAX)
    file_name: str | None = None
    mime: str | None = None
    caption: str | None = None
    local_path: str | None = None
    size: int = 0                # байты, если известно заранее
    is_animated: bool = False
    video_token: str | None = None   # MAX: видео может прийти только токеном


@dataclass
class IncomingMessage:
    platform: str                # 'max' | 'tg'
    chat_id: str
    message_id: str
    sender_id: str
    sender_name: str
    sender_username: str | None
    text: str
    timestamp: int               # как пришло от платформы (сек или мс — нормализуем позже)
    media: list[MediaItem] = field(default_factory=list)
    album_id: str | None = None
    reply_to_source_id: str | None = None
    # Посты каналов и анонимные админы: реального пользователя может не быть
    is_channel_post: bool = False
    author_signature: str | None = None
    # Событие: created | edited | removed
    event: str = "created"
