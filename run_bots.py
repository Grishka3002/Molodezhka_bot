from __future__ import annotations

import os
import threading
import time
import traceback

from src.admin_bot import AdminBot, parse_admin_ids
from src.bot import CONTENT_PATH, DB_PATH, DEFAULT_CONTENT_PATH, ROOT, YouthBot, load_env
from src.content import Content
from src.db import Storage
from src.runtime_lock import RuntimeLock
from src.telegram_api import TelegramAPI


def run_student_bot() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "put_your_token_here":
        raise RuntimeError("Укажите TELEGRAM_BOT_TOKEN в переменных окружения или .env.")

    api = TelegramAPI(token)
    print_bot_identity(api, "Student bot")
    YouthBot(
        api,
        Storage(DB_PATH),
        Content(CONTENT_PATH, DEFAULT_CONTENT_PATH),
        bot_name="student",
    ).run()


def run_admin_bot() -> None:
    token = os.getenv("ADMIN_BOT_TOKEN")
    if not token or token == "put_admin_bot_token_here":
        raise RuntimeError("Укажите ADMIN_BOT_TOKEN в переменных окружения или .env.")

    api = TelegramAPI(token)
    print_bot_identity(api, "Admin bot")
    AdminBot(
        api,
        Storage(DB_PATH),
        Content(CONTENT_PATH, DEFAULT_CONTENT_PATH),
        parse_admin_ids(os.getenv("ADMIN_TELEGRAM_IDS")),
        bot_name="admin",
    ).run()


def print_bot_identity(api: TelegramAPI, label: str) -> None:
    try:
        info = api.get_me()
    except Exception as error:
        print(f"{label} identity check skipped: {error}")
        return
    print(f"{label} identity: @{info.get('username')}")


def guarded_runner(name: str, target) -> None:
    try:
        target()
    except Exception:
        print(f"{name} stopped with an error:")
        traceback.print_exc()


def validate_required_env() -> None:
    missing = []
    for name in ("TELEGRAM_BOT_TOKEN", "ADMIN_BOT_TOKEN", "ADMIN_TELEGRAM_IDS"):
        value = os.getenv(name)
        if not value or value.startswith("put_"):
            missing.append(name)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Не заданы переменные окружения: {joined}")


def main() -> None:
    load_env(ROOT / ".env")
    validate_required_env()
    Content(CONTENT_PATH, DEFAULT_CONTENT_PATH)

    try:
        with RuntimeLock(ROOT / "data" / "student_bot.lock", "Основной бот"), RuntimeLock(
            ROOT / "data" / "admin_bot.lock",
            "Админ-бот",
        ):
            threads = [
                threading.Thread(
                    target=guarded_runner,
                    args=("Основной бот", run_student_bot),
                    name="student-bot",
                    daemon=True,
                ),
                threading.Thread(
                    target=guarded_runner,
                    args=("Админ-бот", run_admin_bot),
                    name="admin-bot",
                    daemon=True,
                ),
            ]

            for thread in threads:
                thread.start()

            print("Both bots started. Press Ctrl+C to stop.")
            while all(thread.is_alive() for thread in threads):
                time.sleep(1)
            for thread in threads:
                if not thread.is_alive():
                    print(f"{thread.name} stopped. Restart the bots after checking the error above.")
    except RuntimeError as error:
        print(error)
    except KeyboardInterrupt:
        print("Stopping bots...")


if __name__ == "__main__":
    main()
