# max_resend_master: MAX ⇄ Telegram Bridge

Two-way message bridge between the MAX messenger and Telegram. A bot in a MAX chat and a bot in
a Telegram group or channel archive messages and files in SQLite on the server, then repost
them to the other platform with a "from / where / when" caption.

The detailed Russian guide, including troubleshooting, is in [README.ru.md](README.ru.md).

## Features

- Text, photos, video, audio, voice notes, documents, stickers and GIFs, in both directions.
- Albums: MAX sends one message, Telegram sends parts that the bridge reassembles.
  Albums are re-split to platform limits (10 items in Telegram, 12 in MAX).
- Replies are preserved through a message-ID mapping.
- Archive of messages and media in SQLite plus files on disk, with SHA-256 hashes.
- Idempotent: duplicate updates are not reposted. Loop protection by mapping and by the bots' own IDs.
- Rate limits: 2 messages/s per chat, about 25 rps overall, ordering kept within a chat.
- Long-text splitting (MAX 4000, Telegram 4096, captions 1024).
- File and media separation and size checks for each platform's limits.
- Retries with exponential backoff on 429, 5xx and `attachment.not.ready`.
- MAX video URL resolution via `GET /videos/{token}`.
- Optional local Telegram Bot API server for files larger than 50 MB.
- TLS trust that includes the Russian Ministry of Digital Development root CA, needed
  for MAX. `install_ru_certs.py` downloads the certificates into `certs/`.
- Diagnostic scripts: `diagnose.py` (Windows, network, TLS), `max_check.py`
  (permissions, chat IDs, live events) and `tg_check.py` (chat ID, rights, privacy mode).
- Workaround for the Windows asyncio `socketpair` issue (`winfix.py`).

## Tech Stack

- Python 3 (asyncio)
- aiogram 3 (Telegram), httpx (MAX Bot API), aiosqlite, python-dotenv
- SQLite

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in tokens and chat IDs
python install_ru_certs.py  # if MAX TLS verification fails
```

## Configuration

Set in `.env` (see `.env.example`):

| Variable | Description |
|---|---|
| `TG_BOT_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | Telegram chat/channel ID |
| `TG_LOCAL_BOT_API`, `TG_API_BASE` | Use a local Bot API server and its base URL |
| `MAX_BOT_TOKEN` | MAX bot token |
| `MAX_CHAT_ID` | MAX chat ID |
| `MAX_BASE_URL` | MAX API base, `https://platform-api2.max.ru` |
| `DB_PATH`, `MEDIA_DIR` | SQLite archive path and media folder |
| `ALBUM_DEBOUNCE` | Seconds to wait while collecting Telegram album parts |
| `BRIDGE_TIMEZONE` | Time zone for captions (default `Europe/Moscow`) |
| `MAX_CA_BUNDLE`, `SSL_VERIFY` | Extra CA bundle and TLS verification switch (debug only) |
| `LOG_LEVEL` | `INFO` or `DEBUG` |

Platform requirements:

- The MAX bot needs the `read_all_messages` permission.
- The Telegram bot must be a group admin or have privacy mode disabled.

## Usage

```bash
python main.py
```

## Project Structure

```
main.py          listeners, album assembly, startup
bridge.py        forwarding core
max_client.py    MAX Bot API REST client
db.py            SQLite: de-duplication, archive, mapping
config.py        configuration from .env
models.py        unified incoming message model
utils.py         time, captions, text splitting, downloads
ratelimit.py     global and per-chat rate limiting
sslsetup.py      TLS trust setup
winfix.py        Windows socketpair workaround
install_ru_certs.py, diagnose.py, max_check.py, tg_check.py   setup and diagnostics
```

## Notes

- MAX updates are received by long polling (`/updates`). Webhook mode is not wired up yet.
- Not supported: Telegram → MAX deletions (no Bot API event), files over 2000 MB to
  Telegram, reactions and polls.
- The bridge stores other people's messages and files. Make sure this complies with personal
  data law (for example, Russian Federal Law 152-FZ) and inform chat members.

## License

Copyright 2026 berlonak. Licensed under the [Apache License, Version 2.0](LICENSE).
