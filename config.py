# copyright by berlonak
# telegram: @Kilax123
"""Конфигурация мостика MAX <-> Telegram."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env.example)")
    return v


@dataclass(frozen=True)
class Config:
    # Telegram
    tg_token: str
    tg_chat_id: int
    tg_api_base: str            # для локального Bot API Server
    # MAX
    max_token: str
    max_chat_id: int
    max_base_url: str
    # Хранилище
    db_path: str
    media_dir: str
    album_debounce: float
    timezone: str
    # Лимиты
    tg_download_limit: int      # cloud API: 20 МБ на скачивание
    tg_upload_limit: int        # cloud API: 50 МБ на отправку
    local_bot_api: bool
    # TLS
    ca_bundle: str | None       # дополнительный файл/папка с корневыми сертификатами
    ssl_verify: bool            # отключать только для отладки


def load_config() -> Config:
    local = os.getenv("TG_LOCAL_BOT_API", "false").lower() in ("1", "true", "yes")
    return Config(
        tg_token=_req("TG_BOT_TOKEN"),
        tg_chat_id=int(_req("TG_CHAT_ID")),
        tg_api_base=os.getenv("TG_API_BASE", "https://api.telegram.org"),
        max_token=_req("MAX_BOT_TOKEN"),
        max_chat_id=int(_req("MAX_CHAT_ID")),
        max_base_url=os.getenv("MAX_BASE_URL", "https://platform-api2.max.ru"),
        db_path=os.getenv("DB_PATH", "bridge.db"),
        media_dir=os.getenv("MEDIA_DIR", "media"),
        album_debounce=float(os.getenv("ALBUM_DEBOUNCE", "1.5")),
        timezone=os.getenv("BRIDGE_TIMEZONE", "Europe/Moscow"),
        # При локальном Bot API Server лимиты снимаются (до 2000 МБ на отправку).
        tg_download_limit=int(os.getenv("TG_DOWNLOAD_LIMIT",
                                        str(2000 * 1024 * 1024 if local else 20 * 1024 * 1024))),
        tg_upload_limit=int(os.getenv("TG_UPLOAD_LIMIT",
                                      str(2000 * 1024 * 1024 if local else 50 * 1024 * 1024))),
        local_bot_api=local,
        ca_bundle=os.getenv("MAX_CA_BUNDLE") or None,
        ssl_verify=os.getenv("SSL_VERIFY", "true").lower() not in ("0", "false", "no"),
    )
