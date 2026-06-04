from __future__ import annotations

import os

from src.bot import ROOT, load_env
from src.telegram_api import TelegramAPI, TelegramError


def check_bot(env_name: str, label: str) -> None:
    token = os.getenv(env_name)
    if not token or token.startswith("put_"):
        print(f"{label}: токен не указан")
        return
    try:
        info = TelegramAPI(token).get_me()
    except TelegramError as error:
        print(f"{label}: ошибка Telegram API: {error}")
        return
    print(f"{label}: OK, @{info.get('username')}")


def main() -> None:
    load_env(ROOT / ".env")
    check_bot("TELEGRAM_BOT_TOKEN", "Основной бот")
    check_bot("ADMIN_BOT_TOKEN", "Админ-бот")


if __name__ == "__main__":
    main()
