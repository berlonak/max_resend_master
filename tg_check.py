# copyright by berlonak
# telegram: @Kilax123
"""Диагностика стороны Telegram: почему не уходит сообщение.

Запуск:
    python tg_check.py           # проверки + прослушивание 90 секунд
    python tg_check.py 30        # прослушивание 30 секунд
    python tg_check.py 0         # только проверки

ВАЖНО: остановите main.py перед запуском — Telegram не разрешает
двум процессам одновременно читать обновления одного бота.
"""
from __future__ import annotations

import asyncio
import json
import sys

import winfix

winfix.apply(verbose=False)

import httpx

from config import load_config
from sslsetup import build_ssl_context


def line(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def dump(obj, limit: int = 1800) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    print(text if len(text) <= limit else text[:limit] + "\n… (обрезано)")


def id_candidates(raw) -> list[str]:
    """Возможные записи одного и того же чата.

    Обычная группа      -> -1234567890
    Супергруппа/канал   -> -1001234567890   (префикс -100)
    Личный чат          ->  1234567890
    """
    s = str(raw).strip()
    digits = s.lstrip("-")
    out = [s]
    if digits.startswith("100") and len(digits) > 3:
        out.append("-" + digits[3:])          # убрать префикс супергруппы
    if not digits.startswith("100"):
        out.append("-100" + digits)           # добавить префикс супергруппы
    out.append("-" + digits)
    out.append(digits)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


CHAT_TYPE_RU = {
    "private": "личный чат",
    "group": "обычная группа",
    "supergroup": "супергруппа",
    "channel": "канал",
}


class Tg:
    def __init__(self, token: str, base: str, verify):
        self.base = f"{base.rstrip('/')}/bot{token}"
        self.http = httpx.AsyncClient(timeout=60, verify=verify)

    async def call(self, method: str, **params):
        r = await self.http.get(f"{self.base}/{method}", params=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "неизвестная ошибка"))
        return data["result"]

    async def close(self):
        await self.http.aclose()


async def main() -> int:
    listen_seconds = 90
    if len(sys.argv) > 1:
        try:
            listen_seconds = int(sys.argv[1])
        except ValueError:
            pass

    cfg = load_config()
    tg = Tg(cfg.tg_token, cfg.tg_api_base, build_ssl_context(cfg.ca_bundle, cfg.ssl_verify))
    problems: list[str] = []
    working_id: str | None = None
    bot_id = None

    try:
        # ---------------- 1. бот ----------------
        line("1. Бот  (getMe)")
        try:
            me = await tg.call("getMe")
            bot_id = me["id"]
            print(f"ID бота  : {bot_id}")
            print(f"Имя      : {me.get('first_name')}")
            print(f"username : @{me.get('username')}")
            print(f"Может читать все сообщения в группах: "
                  f"{me.get('can_read_all_group_messages')}")
            if me.get("can_read_all_group_messages") is False:
                print("\n⚠️  Privacy Mode ВКЛЮЧЁН. В обычной группе бот видит только")
                print("   команды и ответы на свои сообщения — обычные сообщения ему")
                print("   не приходят. Направление Telegram -> MAX работать не будет.")
                problems.append(
                    "Privacy Mode включён. Отключите: @BotFather -> /setprivacy -> Disable, "
                    "затем УДАЛИТЕ бота из группы и добавьте заново (иначе не применится).")
        except Exception as e:
            print(f"❌ {e}")
            return 1

        # ---------------- 2. webhook ----------------
        line("2. Webhook  (getWebhookInfo)")
        print("Webhook и getUpdates взаимоисключающие: если webhook задан,")
        print("мост через long polling ничего не получит.\n")
        try:
            info = await tg.call("getWebhookInfo")
            if info.get("url"):
                print(f"⚠️  Задан webhook: {info['url']}")
                problems.append(
                    "Задан webhook — long polling работать не будет. Снимите его: "
                    "https://api.telegram.org/bot<ТОКЕН>/deleteWebhook")
            else:
                print("✅ Webhook не задан — long polling работает.")
            if info.get("pending_update_count"):
                print(f"   Необработанных обновлений в очереди: {info['pending_update_count']}")
        except Exception as e:
            print(f"(проверить не удалось: {e})")

        # ---------------- 3. подбор chat_id ----------------
        line(f"3. Проверка TG_CHAT_ID={cfg.tg_chat_id}")
        print("Форматы ID в Telegram различаются по типу чата:")
        print("   обычная группа     ->  -1234567890")
        print("   супергруппа/канал  ->  -1001234567890   (префикс -100)")
        print("   личный чат         ->   1234567890\n")

        for cand in id_candidates(cfg.tg_chat_id):
            try:
                chat = await tg.call("getChat", chat_id=cand)
                ctype = chat.get("type")
                mark = "✅" if cand == str(cfg.tg_chat_id) else "✅ ПОДХОДИТ"
                print(f"{mark}  {cand}  ->  «{chat.get('title') or chat.get('first_name')}» "
                      f"({CHAT_TYPE_RU.get(ctype, ctype)})")
                if working_id is None:
                    working_id = cand
                    working_chat = chat
            except Exception as e:
                print(f"❌  {cand}  ->  {e}")

        if working_id is None:
            print("\n❌ Ни один вариант не подошёл.")
            print("   Значит, бот не добавлен в этот чат, либо ID совсем другой.")
            print("   Пункт 6 ниже покажет настоящий ID — просто напишите в группу.")
            problems.append("Ни один вариант TG_CHAT_ID не открывается через getChat.")
        else:
            print(f"\nПолный ответ для {working_id}:")
            dump(working_chat)
            if working_id != str(cfg.tg_chat_id):
                print()
                print("╔" + "═" * 62 + "╗")
                print("║" + "  НАЙДЕНА ПРИЧИНА — ИСПРАВЬТЕ .env".ljust(62) + "║")
                print("╠" + "═" * 62 + "╣")
                print("║" + f"  Сейчас в .env :  TG_CHAT_ID={cfg.tg_chat_id}".ljust(62) + "║")
                print("║" + f"  Нужно записать:  TG_CHAT_ID={working_id}".ljust(62) + "║")
                print("╚" + "═" * 62 + "╝")
                print()
                problems.append(f"ЗАМЕНИТЕ В .env:  TG_CHAT_ID={working_id}")

        # ---------------- 4. права бота ----------------
        if working_id:
            line(f"4. Бот в чате  (getChatMember)")
            try:
                m = await tg.call("getChatMember", chat_id=working_id, user_id=bot_id)
                status = m.get("status")
                print(f"Статус: {status}")
                dump(m)
                if status in ("left", "kicked"):
                    print("\n❌ Бота нет в чате — добавьте его.")
                    problems.append("Бот не состоит в чате (статус: " + status + ").")
                elif status == "administrator":
                    print("\n✅ Бот администратор — видит все сообщения и может писать.")
                else:
                    print("\n⚠️  Бот обычный участник. Писать он сможет, но читать все")
                    print("   сообщения — только если отключён Privacy Mode.")
            except Exception as e:
                print(f"❌ {e}")

        # ---------------- 5. пробная отправка ----------------
        if working_id:
            line("5. Пробная отправка сообщения")
            try:
                sent = await tg.call("sendMessage", chat_id=working_id,
                                     text="✅ Проверка связи от моста MAX ⇄ Telegram")
                print(f"✅ Отправлено, message_id={sent['message_id']}")
                print("   Посмотрите в группу — сообщение должно быть там.")
            except Exception as e:
                print(f"❌ Отправить не удалось: {e}")
                problems.append(f"Отправка не работает: {e}")

        # ---------------- 6. живое прослушивание ----------------
        if listen_seconds > 0:
            line(f"6. Живое прослушивание — {listen_seconds} секунд")
            print("👉 СЕЙЧАС НАПИШИТЕ ЛЮБОЕ СООБЩЕНИЕ В ГРУППУ TELEGRAM.\n")
            print("(Если main.py запущен — остановите его, иначе события уйдут туда.)\n")

            offset = None
            seen: dict[str, str] = {}
            count = 0
            loop = asyncio.get_event_loop()
            deadline = loop.time() + listen_seconds

            while loop.time() < deadline:
                try:
                    remain = max(1, int(deadline - loop.time()))
                    params = {"timeout": min(20, remain),
                              "allowed_updates": json.dumps(["message", "channel_post"])}
                    if offset is not None:
                        params["offset"] = offset
                    updates = await tg.call("getUpdates", **params)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    print("\nПрослушивание прервано — показываю итог.\n")
                    break
                except Exception as e:
                    print(f"   ошибка опроса: {e}")
                    if "terminated by other getUpdates" in str(e):
                        print("   ⚠️  Параллельно работает main.py — остановите его.")
                        break
                    await asyncio.sleep(2)
                    continue

                for upd in updates:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg:
                        continue
                    count += 1
                    chat = msg["chat"]
                    cid, ctype = str(chat["id"]), chat.get("type")
                    seen[cid] = f"«{chat.get('title') or chat.get('first_name')}» " \
                                f"({CHAT_TYPE_RU.get(ctype, ctype)})"
                    who = (msg.get("from") or {}).get("first_name", "?")
                    text = (msg.get("text") or "[без текста]")[:50]
                    print(f"--- Событие #{count} ---")
                    print(f"   chat.id : {cid}   {seen[cid]}")
                    print(f"   от      : {who}")
                    print(f"   текст   : {text}\n")

                if count >= 2:      # достаточно, чтобы подтвердить ID
                    print("Достаточно событий — завершаю прослушивание досрочно.\n")
                    deadline = 0

            print(f"Всего событий: {count}")
            if count == 0:
                print("\n❌ Ничего не пришло.")
                print("   Если вы писали в группу — включён Privacy Mode (см. пункт 1):")
                print("   бот не видит обычные сообщения. Отключите его у @BotFather,")
                print("   затем удалите бота из группы и добавьте заново.")
                problems.append("За время прослушивания не пришло ни одного события.")
            else:
                print("\nНастоящие ID чатов, из которых пришли сообщения:")
                for cid, desc in seen.items():
                    mark = "  <-- совпадает с .env" if cid == str(cfg.tg_chat_id) else ""
                    print(f"   TG_CHAT_ID={cid}   {desc}{mark}")
                if str(cfg.tg_chat_id) not in seen:
                    print(f"\n❌ Ваш TG_CHAT_ID={cfg.tg_chat_id} среди них не встретился.")
                    print("   Возьмите ID из списка выше и впишите в .env.")

        # ---------------- итог ----------------
        line("ИТОГ")
        if not problems:
            print("✅ Проблем не найдено — сторона Telegram настроена верно.")
        else:
            for i, p in enumerate(problems, 1):
                print(f"{i}. {p}")
        if working_id and working_id != str(cfg.tg_chat_id):
            print(f"\n👉 ГЛАВНОЕ: впишите в .env строку\n\n     TG_CHAT_ID={working_id}\n")
    except (KeyboardInterrupt, asyncio.CancelledError):
        line("ПРЕРВАНО")
        if working_id and working_id != str(cfg.tg_chat_id):
            print(f"👉 Но главное уже найдено. Впишите в .env:\n\n     TG_CHAT_ID={working_id}\n")
        else:
            print("Проверка остановлена пользователем.")
    finally:
        await tg.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
