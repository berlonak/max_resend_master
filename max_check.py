# copyright by berlonak
# telegram: @Kilax123
"""Диагностика стороны MAX: почему бот не видит сообщения.

Запуск:
    python max_check.py            # проверки + прослушивание 90 секунд
    python max_check.py 30         # прослушивание 30 секунд
    python max_check.py 0          # только проверки, без прослушивания

Скрипт ничего не пересылает и ничего не меняет — только читает и показывает.
"""
from __future__ import annotations

import asyncio
import json
import sys

import winfix

winfix.apply(verbose=False)

from config import load_config
from max_client import MaxClient, MaxError
from sslsetup import build_ssl_context


def line(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def dump(obj, limit: int = 2500) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    print(text if len(text) <= limit else text[:limit] + "\n… (обрезано)")


def find_chat_ids(node, found: set[str], path: str = "") -> None:
    """Рекурсивно ищет все поля chat_id — чтобы точно знать, где MAX его кладёт."""
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if k == "chat_id" and v is not None:
                found.add(f"{v}  (поле {here})")
            else:
                find_chat_ids(v, found, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            find_chat_ids(v, found, f"{path}[{i}]")


async def main() -> int:
    listen_seconds = 90
    if len(sys.argv) > 1:
        try:
            listen_seconds = int(sys.argv[1])
        except ValueError:
            pass

    cfg = load_config()
    client = MaxClient(cfg.max_token, cfg.max_base_url,
                       verify=build_ssl_context(cfg.ca_bundle, cfg.ssl_verify))
    problems: list[str] = []

    try:
        # ---------------- 1. бот ----------------
        line("1. Бот  (GET /me)")
        try:
            me = await client.get_me()
            print(f"ID бота    : {client.bot_id}")
            print(f"Имя        : {me.get('name') or me.get('first_name')}")
            print(f"username   : {me.get('username')}")
            print("\nПолный ответ:")
            dump(me)
        except MaxError as e:
            print(f"❌ {e}")
            return 1

        # ---------------- 2. webhook-подписки ----------------
        line("2. Webhook-подписки  (GET /subscriptions)")
        print("Важно: long polling и webhook несовместимы. Если подписка активна,")
        print("MAX шлёт события на неё, и /updates остаётся пустым.\n")
        try:
            subs = await client.get_subscriptions()
            items = subs.get("subscriptions", subs) if isinstance(subs, dict) else subs
            if items:
                print("⚠️  НАЙДЕНЫ АКТИВНЫЕ ПОДПИСКИ — вероятно, это и есть причина:")
                dump(items)
                problems.append(
                    "Активна webhook-подписка. Пока она есть, long polling может не "
                    "получать события. Удалите её или переходите на webhook.")
            else:
                print("✅ Подписок нет — long polling должен работать.")
        except MaxError as e:
            print(f"(не удалось проверить: {e})")

        # ---------------- 3. чат из .env ----------------
        line(f"3. Чат из .env  (GET /chats/{cfg.max_chat_id})")
        chat_ok = False
        try:
            chat = await client.get_chat(cfg.max_chat_id)
            chat_ok = True
            print(f"Название   : {chat.get('title')}")
            print(f"Тип        : {chat.get('type')}   (chat=группа, channel=канал, dialog=личка)")
            print(f"Статус бота: {chat.get('status')}")
            print(f"Участников : {chat.get('participants_count')}")
            print("\nПолный ответ:")
            dump(chat)
            if chat.get("status") not in (None, "active"):
                problems.append(f"Статус бота в чате: {chat.get('status')} (ожидается active).")
        except MaxError as e:
            print(f"❌ Чат не найден или недоступен: {e}")
            problems.append(f"MAX_CHAT_ID={cfg.max_chat_id} не открывается. Проверьте ID.")

        # ---------------- 4. права бота ----------------
        line(f"4. Права бота в чате  (GET /chats/{cfg.max_chat_id}/members/me)")
        if chat_ok:
            try:
                member = await client.get_my_membership(cfg.max_chat_id)
                print("Полный ответ:")
                dump(member)

                perms = member.get("permissions")
                print()
                if perms is None:
                    print("⚠️  Поле permissions отсутствует в ответе.")
                    print("   Обычно это значит, что бот НЕ администратор чата.")
                    problems.append(
                        "В ответе нет permissions — бот, скорее всего, не админ. "
                        "Назначьте его администратором и выдайте read_all_messages.")
                elif "read_all_messages" in (perms or []):
                    print("✅ Право read_all_messages есть.")
                else:
                    print(f"❌ Права: {perms}")
                    print("   Права read_all_messages НЕТ — событий о сообщениях не будет.")
                    problems.append(
                        "Нет права read_all_messages. Это самая частая причина, "
                        "почему бот сидит в группе и молчит.")

                if member.get("is_admin") is False:
                    print("⚠️  is_admin = false — бот не администратор.")
            except MaxError as e:
                print(f"❌ {e}")

        # ---------------- 5. чтение сообщений ----------------
        line(f"5. Может ли бот читать сообщения  (GET /messages?chat_id={cfg.max_chat_id})")
        print("Прямая проверка доступа к содержимому чата.\n")
        try:
            data = await client.get_messages(cfg.max_chat_id, count=5)
            msgs = data.get("messages", []) if isinstance(data, dict) else []
            if msgs:
                print(f"✅ Бот видит сообщения — получено {len(msgs)} шт. Доступ к чтению ЕСТЬ.\n")
                found: set[str] = set()
                for m in msgs[:3]:
                    body = m.get("body", {}) or {}
                    sender = m.get("sender", {}) or {}
                    text = (body.get("text") or "").replace("\n", " ")[:60]
                    print(f"   • от {sender.get('name', '?')}: {text or '[без текста]'}")
                    find_chat_ids(m, found)
                print("\nГде в сообщении лежит chat_id:")
                for f in sorted(found):
                    print(f"   {f}")
                print("\nСтруктура первого сообщения (для сверки парсера):")
                dump(msgs[0], 1800)
            else:
                print("⚠️  Сообщений не вернулось.")
                print("   Либо чат пуст, либо у бота нет доступа к содержимому.")
                problems.append("GET /messages вернул пусто — проверьте права на чтение.")
        except MaxError as e:
            print(f"❌ {e}")
            problems.append(f"Чтение сообщений недоступно: {e}")

        # ---------------- 6. живое прослушивание ----------------
        if listen_seconds > 0:
            line(f"6. Живое прослушивание — {listen_seconds} секунд")
            print("👉 СЕЙЧАС НАПИШИТЕ ЛЮБОЕ СООБЩЕНИЕ В ГРУППУ MAX.\n")
            print(f"Ваш MAX_CHAT_ID из .env: {cfg.max_chat_id}\n")

            seen_chats: set[str] = set()
            count = 0
            marker = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + listen_seconds

            while loop.time() < deadline:
                try:
                    remain = max(1, int(deadline - loop.time()))
                    data = await client.get_updates(marker, timeout=min(20, remain))
                except (KeyboardInterrupt, asyncio.CancelledError):
                    print("\nПрослушивание прервано — показываю итог.\n")
                    break
                except MaxError as e:
                    print(f"   ошибка опроса: {e}")
                    await asyncio.sleep(2)
                    continue

                for upd in data.get("updates", []) or []:
                    count += 1
                    utype = upd.get("update_type")
                    print(f"\n--- Событие #{count}: {utype} ---")
                    dump(upd, 1500)
                    find_chat_ids(upd, seen_chats)
                marker = data.get("marker", marker)

                if count >= 2:      # достаточно для подтверждения
                    print("\nДостаточно событий — завершаю прослушивание досрочно.\n")
                    deadline = 0

            print(f"\n\nВсего событий получено: {count}")
            if count == 0:
                print("\n❌ НИ ОДНОГО события не пришло.")
                print("   Если вы точно писали в группу — у бота нет права")
                print("   read_all_messages, либо активна webhook-подписка.")
                problems.append("За время прослушивания не пришло ни одного события.")
            else:
                print("\nВсе chat_id, встреченные в событиях:")
                for c in sorted(seen_chats):
                    print(f"   {c}")
                raw_ids = {s.split()[0] for s in seen_chats}
                if str(cfg.max_chat_id) in raw_ids:
                    print(f"\n✅ Ваш MAX_CHAT_ID={cfg.max_chat_id} встречается в событиях — ID верный.")
                else:
                    print(f"\n❌ MAX_CHAT_ID={cfg.max_chat_id} в событиях НЕ встретился!")
                    print("   Возьмите нужный ID из списка выше и впишите в .env.")
                    problems.append(
                        f"MAX_CHAT_ID={cfg.max_chat_id} не совпадает с ID из событий.")

        # ---------------- итог ----------------
        line("ИТОГ")
        if not problems:
            print("✅ Явных проблем не найдено.")
            print("   Если события приходили и ID совпал — мост должен работать.")
        else:
            for i, p in enumerate(problems, 1):
                print(f"{i}. {p}")
            print("""
Как выдать боту право read_all_messages:
  1. Откройте группу/канал в MAX;
  2. Настройки чата -> Участники -> найдите бота;
  3. Назначьте администратором;
  4. Включите доступ к чтению всех сообщений;
  5. Перезапустите мост.

Без этого права бот состоит в чате, но событий о сообщениях не получает —
это самая частая причина «в личке отвечает, группу не видит».""")
    except (KeyboardInterrupt, asyncio.CancelledError):
        line("ПРЕРВАНО")
        if problems:
            print("Найденные проблемы:")
            for i, p_ in enumerate(problems, 1):
                print(f"{i}. {p_}")
        else:
            print("Проверка остановлена пользователем.")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
