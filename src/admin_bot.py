from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from .bot import CONTENT_PATH, DB_PATH, ROOT, clean, inline_keyboard, load_env
from .content import Content
from .db import Storage
from .runtime_lock import RuntimeLock
from .telegram_api import TelegramAPI, TelegramError


MESSAGE_LABELS = {
    "ask_full_name": "Вопрос: ФИО",
    "ask_phone": "Вопрос: телефон",
    "ask_age": "Вопрос: возраст",
    "ask_gender": "Вопрос: пол",
    "ask_interests": "Вопрос: интересы",
    "ask_direction": "Вопрос: направление",
    "invalid_full_name": "Ошибка: ФИО",
    "invalid_phone": "Ошибка: телефон",
    "invalid_age_format": "Ошибка: возраст не число",
    "invalid_age_range": "Ошибка: возраст вне диапазона",
    "invalid_interests": "Ошибка: интересы",
    "invalid_direction": "Ошибка: направление",
    "profile_complete": "Анкета готова",
    "main_choose": "Главное меню",
    "recommendations_after_profile": "Рекомендации после анкеты",
    "recommendations_intro": "Рекомендации",
    "recommendations_empty": "Рекомендации пусто",
    "popular_intro": "Рекомендации, старый ключ",
    "popular_empty": "Рекомендации пусто, старый ключ",
    "categories_intro": "Выбор активностей",
    "category_choose": "Выбор объединения",
    "unknown_section": "Неизвестный раздел",
    "profile": "Экран профиля",
}


ITEM_FIELDS = {
    "title": "Название",
    "description": "Описание",
    "contact": "Контакты",
    "where": "Где и когда",
}


class AdminBot:
    def __init__(
        self,
        api: TelegramAPI,
        storage: Storage,
        content: Content,
        admin_ids: set[int],
        bot_name: str = "admin",
    ) -> None:
        self.api = api
        self.storage = storage
        self.content = content
        self.admin_ids = admin_ids
        self.bot_name = bot_name

    def run(self) -> None:
        print("Admin bot started. Press Ctrl+C to stop.")
        offset = self.start_offset()
        error_count = 0
        while True:
            try:
                updates = self.api.get_updates(offset=offset)
                if error_count:
                    print("Admin Telegram connection restored.")
                error_count = 0
                if not updates:
                    time.sleep(0.35)
                    continue
                for update in updates:
                    offset = update["update_id"] + 1
                    self.storage.set_bot_offset(self.bot_name, offset)
                    try:
                        self.log_update(update)
                        self.handle_update(update)
                    except Exception:
                        print(f"Admin bot failed on update {update.get('update_id')}:")
                        traceback.print_exc()
            except TelegramError as error:
                error_count += 1
                delay = min(60, 3 * error_count)
                if error_count == 1 or error_count % 5 == 0:
                    print(f"Admin Telegram connection problem, retry in {delay}s: {error}")
                time.sleep(delay)
            except KeyboardInterrupt:
                print("Admin bot stopped.")
                return

    def start_offset(self) -> int | None:
        offset = self.storage.get_bot_offset(self.bot_name)
        if offset is not None:
            return offset

        try:
            pending = self.api.get_updates(offset=None)
        except TelegramError as error:
            print(f"Admin bot could not check old pending updates at startup: {error}")
            return None

        if not pending:
            return None

        offset = max(update["update_id"] for update in pending) + 1
        self.storage.set_bot_offset(self.bot_name, offset)
        print(f"Admin bot skipped {len(pending)} old pending updates.")
        return offset

    def log_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            text = (update["message"].get("text") or "").strip()
            print(f"Admin update {update['update_id']}: message {text[:40]!r}")
        elif "callback_query" in update:
            data = update["callback_query"].get("data") or ""
            print(f"Admin update {update['update_id']}: callback {data[:40]!r}")

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        telegram_id = message["from"]["id"]
        text = (message.get("text") or "").strip()
        photo_file_id = self.photo_file_id(message)

        if text.startswith("/id"):
            self.send(chat_id, f"Ваш Telegram ID: <code>{telegram_id}</code>")
            return

        if not self.is_admin(telegram_id):
            self.send_not_allowed(chat_id, telegram_id)
            return

        if text.startswith("/start") or text.startswith("/admin"):
            self.storage.clear_admin_session(telegram_id)
            self.show_main(chat_id)
            return
        if text.startswith("/cancel"):
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Действие отменено.")
            self.show_main(chat_id)
            return

        state, payload = self.storage.get_admin_session(telegram_id)
        if state:
            self.process_state(chat_id, telegram_id, state, payload, text, photo_file_id)
            return

        self.show_main(chat_id)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        data = callback.get("data") or ""
        message = callback["message"]
        chat_id = message["chat"]["id"]
        telegram_id = callback["from"]["id"]

        self.safe_answer_callback_async(callback_id)

        if not self.is_admin(telegram_id):
            self.send_not_allowed(chat_id, telegram_id)
            return

        self.storage.clear_admin_session(telegram_id)

        if data == "admin:main":
            self.show_main(chat_id, message["message_id"])
        elif data == "admin:intro":
            self.show_intro(chat_id, message["message_id"])
        elif data == "admin:messages":
            self.show_messages(chat_id, message["message_id"])
        elif data == "admin:categories":
            self.show_categories(chat_id, message["message_id"])
        elif data == "admin:directions":
            self.show_directions(chat_id, message["message_id"])
        elif data == "admin:interests":
            self.show_interests(chat_id, message["message_id"])
        elif data == "admin:add_category":
            self.ask_for_value(chat_id, telegram_id, "add_category", {}, "Введите название нового раздела:")
        elif data == "admin:add_direction":
            self.ask_for_value(chat_id, telegram_id, "add_direction", {}, "Введите новое направление обучения:")
        elif data == "admin:add_interest":
            self.ask_for_value(chat_id, telegram_id, "add_interest", {}, "Введите новый интерес:")
        elif data.startswith("intro:"):
            field = data.split(":", 1)[1]
            self.ask_for_value(
                chat_id,
                telegram_id,
                "edit_intro",
                {"field": field},
                f"Введите новое значение для поля «{field}»:",
            )
        elif data == "photo:intro:set":
            self.ask_for_photo(chat_id, telegram_id, {"target": "intro"})
        elif data == "photo:intro:delete":
            self.content.data["intro"]["photo"] = ""
            self.content.save()
            self.show_intro(chat_id, message["message_id"])
        elif data.startswith("msg:"):
            key = data.split(":", 1)[1]
            self.show_message(chat_id, key, message["message_id"])
        elif data.startswith("msgtext:"):
            key = data.split(":", 1)[1]
            label = MESSAGE_LABELS.get(key, key)
            self.ask_for_value(
                chat_id,
                telegram_id,
                "edit_message",
                {"key": key},
                f"Введите новый текст для «{label}»:",
            )
        elif data.startswith("msgphoto:"):
            _, key, action = data.split(":", 2)
            if action == "set":
                self.ask_for_photo(chat_id, telegram_id, {"target": "message", "key": key})
            elif action == "delete":
                self.content.data.setdefault("message_photos", {}).pop(key, None)
                self.content.save()
                self.show_message(chat_id, key, message["message_id"])
        elif data.startswith("direction:"):
            self.show_direction(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("directionedit:"):
            index = int(data.split(":", 1)[1])
            self.ask_for_value(
                chat_id,
                telegram_id,
                "edit_direction",
                {"index": index},
                "Введите новое название направления:",
            )
        elif data.startswith("directiondelete:"):
            self.confirm_delete_direction(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("directiondeleteok:"):
            self.delete_direction(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("interest:"):
            self.show_interest(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("interestedit:"):
            index = int(data.split(":", 1)[1])
            self.ask_for_value(
                chat_id,
                telegram_id,
                "edit_interest",
                {"index": index},
                "Введите новое название интереса:",
            )
        elif data.startswith("interestdelete:"):
            self.confirm_delete_interest(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("interestdeleteok:"):
            self.delete_interest(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("cat:"):
            self.show_category(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("catfield:"):
            _, raw_index, field = data.split(":", 2)
            self.ask_for_value(
                chat_id,
                telegram_id,
                "edit_category",
                {"index": int(raw_index), "field": field},
                f"Введите новое значение для поля «{field}»:",
            )
        elif data.startswith("catphoto:"):
            _, raw_index, action = data.split(":", 2)
            index = int(raw_index)
            if action == "set":
                self.ask_for_photo(chat_id, telegram_id, {"target": "category", "index": index})
            elif action == "delete":
                category = self.category_by_index(index)
                if category:
                    category["photo"] = ""
                    self.content.save()
                self.show_category(chat_id, index, message["message_id"])
        elif data.startswith("catdelete:"):
            self.confirm_delete_category(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("catdeleteok:"):
            self.delete_category(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("items:"):
            self.show_items(chat_id, int(data.split(":", 1)[1]), message["message_id"])
        elif data.startswith("additem:"):
            category_index = int(data.split(":", 1)[1])
            self.ask_for_value(
                chat_id,
                telegram_id,
                "add_item",
                {"category_index": category_index},
                "Введите название нового объединения/секции:",
            )
        elif data.startswith("item:"):
            _, raw_category_index, raw_item_index = data.split(":", 2)
            self.show_item(
                chat_id,
                int(raw_category_index),
                int(raw_item_index),
                message["message_id"],
            )
        elif data.startswith("itemfield:"):
            _, raw_category_index, raw_item_index, field = data.split(":", 3)
            label = ITEM_FIELDS.get(field, field)
            self.ask_for_value(
                chat_id,
                telegram_id,
                "edit_item",
                {
                    "category_index": int(raw_category_index),
                    "item_index": int(raw_item_index),
                    "field": field,
                },
                f"Введите новое значение для поля «{label}»:",
            )
        elif data.startswith("itemphoto:"):
            _, raw_category_index, raw_item_index, action = data.split(":", 3)
            category_index = int(raw_category_index)
            item_index = int(raw_item_index)
            if action == "set":
                self.ask_for_photo(
                    chat_id,
                    telegram_id,
                    {"target": "item", "category_index": category_index, "item_index": item_index},
                )
            elif action == "delete":
                item = self.item_by_index(category_index, item_index)
                if item:
                    item["photo"] = ""
                    self.content.save()
                self.show_item(chat_id, category_index, item_index, message["message_id"])
        elif data.startswith("itemdelete:"):
            _, raw_category_index, raw_item_index = data.split(":", 2)
            self.confirm_delete_item(
                chat_id,
                int(raw_category_index),
                int(raw_item_index),
                message["message_id"],
            )
        elif data.startswith("itemdeleteok:"):
            _, raw_category_index, raw_item_index = data.split(":", 2)
            self.delete_item(
                chat_id,
                int(raw_category_index),
                int(raw_item_index),
                message["message_id"],
            )
        else:
            self.show_main(chat_id, message["message_id"])

    def process_state(
        self,
        chat_id: int,
        telegram_id: int,
        state: str,
        payload: dict[str, Any],
        text: str,
        photo_file_id: str | None,
    ) -> None:
        if state == "set_photo":
            if not photo_file_id:
                self.send(chat_id, "Отправьте именно картинку. Чтобы отменить действие, отправьте /cancel.")
                return
            self.set_photo(payload, photo_file_id)
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Фото обновлено.")
            self.return_after_photo_edit(chat_id, payload)
            return

        if not text:
            self.send(chat_id, "Текст не должен быть пустым. Отправьте значение еще раз или /cancel.")
            return

        self.content.reload_if_changed()

        if state == "edit_intro":
            self.content.data["intro"][payload["field"]] = text
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Приветствие обновлено.")
            self.show_intro(chat_id)
        elif state == "edit_message":
            self.content.data.setdefault("messages", {})[payload["key"]] = text
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Текст бота обновлен.")
            self.show_messages(chat_id)
        elif state == "edit_category":
            category = self.category_by_index(payload["index"])
            if not category:
                self.fail_and_reset(chat_id, telegram_id)
                return
            category[payload["field"]] = text
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Раздел обновлен.")
            self.show_category(chat_id, payload["index"])
        elif state == "add_category":
            self.content.data["categories"].append(
                {
                    "id": self.make_id("category", text),
                    "title": text,
                    "description": "Добавьте описание раздела.",
                    "photo": "",
                    "items": [],
                }
            )
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Раздел добавлен.")
            self.show_categories(chat_id)
        elif state == "add_item":
            category = self.category_by_index(payload["category_index"])
            if not category:
                self.fail_and_reset(chat_id, telegram_id)
                return
            category["items"].append(
                {
                    "id": self.make_id("item", text),
                    "title": text,
                    "description": "Добавьте описание объединения.",
                    "photo": "",
                    "contact": "Добавьте контакты для записи.",
                    "where": "Добавьте место, расписание или ссылку на мероприятия.",
                }
            )
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Объединение добавлено.")
            self.show_items(chat_id, payload["category_index"])
        elif state == "edit_item":
            item = self.item_by_index(payload["category_index"], payload["item_index"])
            if not item:
                self.fail_and_reset(chat_id, telegram_id)
                return
            item[payload["field"]] = text
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Объединение обновлено.")
            self.show_item(chat_id, payload["category_index"], payload["item_index"])
        elif state == "add_direction":
            self.content.data.setdefault("directions", []).append(text)
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Направление добавлено.")
            self.show_directions(chat_id)
        elif state == "edit_direction":
            directions = self.content.data.setdefault("directions", [])
            index = payload["index"]
            if index < 0 or index >= len(directions):
                self.fail_and_reset(chat_id, telegram_id)
                return
            directions[index] = text
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Направление обновлено.")
            self.show_directions(chat_id)
        elif state == "add_interest":
            self.content.data.setdefault("interests", []).append(text)
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Интерес добавлен.")
            self.show_interests(chat_id)
        elif state == "edit_interest":
            interests = self.content.data.setdefault("interests", [])
            index = payload["index"]
            if index < 0 or index >= len(interests):
                self.fail_and_reset(chat_id, telegram_id)
                return
            interests[index] = text
            self.content.save()
            self.storage.clear_admin_session(telegram_id)
            self.send(chat_id, "Интерес обновлен.")
            self.show_interests(chat_id)
        else:
            self.fail_and_reset(chat_id, telegram_id)

    def show_main(self, chat_id: int, message_id: int | None = None) -> None:
        text = (
            "<b>Админ-бот Молодежь ВВГУ</b>\n\n"
            "Здесь можно менять приветствие, тексты анкеты, направления, интересы, разделы, объединения, контакты и фото."
        )
        keyboard = inline_keyboard(
            [
                [("Приветствие", "admin:intro")],
                [("Тексты бота", "admin:messages")],
                [("Направления обучения", "admin:directions")],
                [("Интересы анкеты", "admin:interests")],
                [("Разделы и объединения", "admin:categories")],
            ]
        )
        self.render(chat_id, text, keyboard, message_id)

    def show_intro(self, chat_id: int, message_id: int | None = None) -> None:
        intro = self.content.intro
        text = (
            "<b>Приветствие</b>\n\n"
            f"<b>Заголовок:</b>\n{clean(intro.get('title'))}\n\n"
            f"<b>Текст:</b>\n{clean(intro.get('text'))}\n\n"
            f"<b>Фото:</b> {self.photo_status(intro.get('photo'))}"
        )
        keyboard = inline_keyboard(
            [
                [("Изменить заголовок", "intro:title")],
                [("Изменить текст", "intro:text")],
                [("Загрузить фото", "photo:intro:set")],
                [("Удалить фото", "photo:intro:delete")],
                [("Назад", "admin:main")],
            ]
        )
        self.render(chat_id, text, keyboard, message_id)

    def show_messages(self, chat_id: int, message_id: int | None = None) -> None:
        rows = [[(label, f"msg:{key}")] for key, label in MESSAGE_LABELS.items()]
        rows.append([("Назад", "admin:main")])
        self.render(chat_id, "<b>Тексты бота</b>\n\nВыберите сообщение для редактирования.", inline_keyboard(rows), message_id)

    def show_message(self, chat_id: int, key: str, message_id: int | None = None) -> None:
        label = MESSAGE_LABELS.get(key, key)
        text = (
            f"<b>{clean(label)}</b>\n\n"
            f"<b>Ключ:</b> <code>{clean(key)}</code>\n\n"
            f"<b>Текст:</b>\n{clean(self.content.message(key, ''))}\n\n"
            f"<b>Фото:</b> {self.photo_status(self.content.message_photo(key))}"
        )
        keyboard = inline_keyboard(
            [
                [("Изменить текст", f"msgtext:{key}")],
                [("Загрузить фото", f"msgphoto:{key}:set")],
                [("Удалить фото", f"msgphoto:{key}:delete")],
                [("К текстам", "admin:messages")],
            ]
        )
        self.render(chat_id, text, keyboard, message_id)

    def show_directions(self, chat_id: int, message_id: int | None = None) -> None:
        directions = self.content.directions
        rows = [[(direction, f"direction:{index}")] for index, direction in enumerate(directions)]
        rows.append([("Добавить направление", "admin:add_direction")])
        rows.append([("Назад", "admin:main")])
        text = "<b>Направления обучения</b>\n\nЭти варианты студент увидит кнопками при заполнении анкеты."
        self.render(chat_id, text, inline_keyboard(rows), message_id)

    def show_direction(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        directions = self.content.directions
        if index < 0 or index >= len(directions):
            self.show_directions(chat_id, message_id)
            return
        text = f"<b>Направление</b>\n\n{clean(directions[index])}"
        keyboard = inline_keyboard(
            [
                [("Изменить", f"directionedit:{index}")],
                [("Удалить", f"directiondelete:{index}")],
                [("К направлениям", "admin:directions")],
            ]
        )
        self.render(chat_id, text, keyboard, message_id)

    def show_interests(self, chat_id: int, message_id: int | None = None) -> None:
        interests = self.content.interests
        rows = [[(interest, f"interest:{index}")] for index, interest in enumerate(interests)]
        rows.append([("Добавить интерес", "admin:add_interest")])
        rows.append([("Назад", "admin:main")])
        text = "<b>Интересы анкеты</b>\n\nЭти варианты студент сможет выбрать при заполнении анкеты."
        self.render(chat_id, text, inline_keyboard(rows), message_id)

    def show_interest(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        interests = self.content.interests
        if index < 0 or index >= len(interests):
            self.show_interests(chat_id, message_id)
            return
        text = f"<b>Интерес</b>\n\n{clean(interests[index])}"
        keyboard = inline_keyboard(
            [
                [("Изменить", f"interestedit:{index}")],
                [("Удалить", f"interestdelete:{index}")],
                [("К интересам", "admin:interests")],
            ]
        )
        self.render(chat_id, text, keyboard, message_id)

    def show_categories(self, chat_id: int, message_id: int | None = None) -> None:
        rows = [[(category["title"], f"cat:{index}")] for index, category in enumerate(self.content.categories)]
        rows.append([("Добавить раздел", "admin:add_category")])
        rows.append([("Назад", "admin:main")])
        self.render(chat_id, "<b>Разделы</b>\n\nВыберите раздел для редактирования.", inline_keyboard(rows), message_id)

    def show_category(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        category = self.category_by_index(index)
        if not category:
            self.show_categories(chat_id, message_id)
            return
        text = (
            f"<b>{clean(category['title'])}</b>\n\n"
            f"<b>ID:</b> <code>{clean(category['id'])}</code>\n\n"
            f"<b>Описание:</b>\n{clean(category['description'])}\n\n"
            f"<b>Фото:</b> {self.photo_status(category.get('photo'))}\n\n"
            f"Объединений: {len(category.get('items', []))}"
        )
        keyboard = inline_keyboard(
            [
                [("Изменить название", f"catfield:{index}:title")],
                [("Изменить описание", f"catfield:{index}:description")],
                [("Загрузить фото", f"catphoto:{index}:set")],
                [("Удалить фото", f"catphoto:{index}:delete")],
                [("Объединения", f"items:{index}")],
                [("Удалить раздел", f"catdelete:{index}")],
                [("К разделам", "admin:categories")],
            ]
        )
        self.render(chat_id, text, keyboard, message_id)

    def show_items(self, chat_id: int, category_index: int, message_id: int | None = None) -> None:
        category = self.category_by_index(category_index)
        if not category:
            self.show_categories(chat_id, message_id)
            return
        rows = [
            [(item["title"], f"item:{category_index}:{index}")]
            for index, item in enumerate(category.get("items", []))
        ]
        rows.append([("Добавить объединение", f"additem:{category_index}")])
        rows.append([("Назад к разделу", f"cat:{category_index}")])
        self.render(
            chat_id,
            f"<b>Объединения: {clean(category['title'])}</b>\n\nВыберите карточку для редактирования.",
            inline_keyboard(rows),
            message_id,
        )

    def show_item(
        self,
        chat_id: int,
        category_index: int,
        item_index: int,
        message_id: int | None = None,
    ) -> None:
        item = self.item_by_index(category_index, item_index)
        if not item:
            self.show_items(chat_id, category_index, message_id)
            return
        text = (
            f"<b>{clean(item['title'])}</b>\n\n"
            f"<b>ID:</b> <code>{clean(item['id'])}</code>\n\n"
            f"<b>Описание:</b>\n{clean(item.get('description'))}\n\n"
            f"<b>Где и когда:</b>\n{clean(item.get('where'))}\n\n"
            f"<b>Контакты:</b>\n{clean(item.get('contact'))}\n\n"
            f"<b>Фото:</b> {self.photo_status(item.get('photo'))}"
        )
        keyboard = inline_keyboard(
            [
                [(label, f"itemfield:{category_index}:{item_index}:{field}")]
                for field, label in ITEM_FIELDS.items()
            ]
            + [
                [("Загрузить фото", f"itemphoto:{category_index}:{item_index}:set")],
                [("Удалить фото", f"itemphoto:{category_index}:{item_index}:delete")],
                [("Удалить объединение", f"itemdelete:{category_index}:{item_index}")],
                [("К объединениям", f"items:{category_index}")],
            ]
        )
        self.render(chat_id, text, keyboard, message_id)

    def confirm_delete_category(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        category = self.category_by_index(index)
        if not category:
            self.show_categories(chat_id, message_id)
            return
        keyboard = inline_keyboard(
            [
                [("Да, удалить", f"catdeleteok:{index}")],
                [("Отмена", f"cat:{index}")],
            ]
        )
        self.render(chat_id, f"Удалить раздел «{clean(category['title'])}»?", keyboard, message_id)

    def delete_category(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        if self.category_by_index(index):
            del self.content.data["categories"][index]
            self.content.save()
        self.show_categories(chat_id, message_id)

    def confirm_delete_item(
        self,
        chat_id: int,
        category_index: int,
        item_index: int,
        message_id: int | None = None,
    ) -> None:
        item = self.item_by_index(category_index, item_index)
        if not item:
            self.show_items(chat_id, category_index, message_id)
            return
        keyboard = inline_keyboard(
            [
                [("Да, удалить", f"itemdeleteok:{category_index}:{item_index}")],
                [("Отмена", f"item:{category_index}:{item_index}")],
            ]
        )
        self.render(chat_id, f"Удалить объединение «{clean(item['title'])}»?", keyboard, message_id)

    def delete_item(
        self,
        chat_id: int,
        category_index: int,
        item_index: int,
        message_id: int | None = None,
    ) -> None:
        category = self.category_by_index(category_index)
        if category and self.item_by_index(category_index, item_index):
            del category["items"][item_index]
            self.content.save()
        self.show_items(chat_id, category_index, message_id)

    def confirm_delete_direction(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        directions = self.content.directions
        if index < 0 or index >= len(directions):
            self.show_directions(chat_id, message_id)
            return
        keyboard = inline_keyboard(
            [
                [("Да, удалить", f"directiondeleteok:{index}")],
                [("Отмена", f"direction:{index}")],
            ]
        )
        self.render(chat_id, f"Удалить направление «{clean(directions[index])}»?", keyboard, message_id)

    def delete_direction(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        directions = self.content.data.setdefault("directions", [])
        if 0 <= index < len(directions):
            del directions[index]
            self.content.save()
        self.show_directions(chat_id, message_id)

    def confirm_delete_interest(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        interests = self.content.interests
        if index < 0 or index >= len(interests):
            self.show_interests(chat_id, message_id)
            return
        keyboard = inline_keyboard(
            [
                [("Да, удалить", f"interestdeleteok:{index}")],
                [("Отмена", f"interest:{index}")],
            ]
        )
        self.render(chat_id, f"Удалить интерес «{clean(interests[index])}»?", keyboard, message_id)

    def delete_interest(self, chat_id: int, index: int, message_id: int | None = None) -> None:
        interests = self.content.data.setdefault("interests", [])
        if 0 <= index < len(interests):
            del interests[index]
            self.content.save()
        self.show_interests(chat_id, message_id)

    def ask_for_value(
        self,
        chat_id: int,
        telegram_id: int,
        state: str,
        payload: dict[str, Any],
        text: str,
    ) -> None:
        self.storage.set_admin_session(telegram_id, state, payload)
        self.send(chat_id, f"{clean(text)}\n\nЧтобы отменить действие, отправьте /cancel.")

    def ask_for_photo(self, chat_id: int, telegram_id: int, payload: dict[str, Any]) -> None:
        self.storage.set_admin_session(telegram_id, "set_photo", payload)
        self.send(chat_id, "Отправьте картинку одним сообщением.\n\nЧтобы отменить действие, отправьте /cancel.")

    def set_photo(self, payload: dict[str, Any], file_id: str) -> None:
        target = payload["target"]
        if target == "intro":
            self.content.data.setdefault("intro", {})["photo"] = file_id
        elif target == "message":
            self.content.data.setdefault("message_photos", {})[payload["key"]] = file_id
        elif target == "category":
            category = self.category_by_index(payload["index"])
            if category:
                category["photo"] = file_id
        elif target == "item":
            item = self.item_by_index(payload["category_index"], payload["item_index"])
            if item:
                item["photo"] = file_id

    def return_after_photo_edit(self, chat_id: int, payload: dict[str, Any]) -> None:
        target = payload["target"]
        if target == "intro":
            self.show_intro(chat_id)
        elif target == "message":
            self.show_message(chat_id, payload["key"])
        elif target == "category":
            self.show_category(chat_id, payload["index"])
        elif target == "item":
            self.show_item(chat_id, payload["category_index"], payload["item_index"])
        else:
            self.show_main(chat_id)

    def category_by_index(self, index: int) -> dict[str, Any] | None:
        categories = self.content.categories
        if index < 0 or index >= len(categories):
            return None
        return categories[index]

    def item_by_index(self, category_index: int, item_index: int) -> dict[str, Any] | None:
        category = self.category_by_index(category_index)
        if not category:
            return None
        items = category.get("items", [])
        if item_index < 0 or item_index >= len(items):
            return None
        return items[item_index]

    def make_id(self, prefix: str, text: str) -> str:
        return f"{prefix}_{int(time.time() * 1000)}"

    def photo_status(self, photo: str | None) -> str:
        return "загружено" if photo else "не загружено"

    def photo_file_id(self, message: dict[str, Any]) -> str | None:
        photos = message.get("photo") or []
        if not photos:
            return None
        return photos[-1]["file_id"]

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids

    def send_not_allowed(self, chat_id: int, telegram_id: int) -> None:
        text = (
            "Доступ к админке закрыт.\n\n"
            f"Ваш Telegram ID: <code>{telegram_id}</code>\n\n"
            "Добавьте этот ID в ADMIN_TELEGRAM_IDS в файле .env."
        )
        self.send(chat_id, text)

    def fail_and_reset(self, chat_id: int, telegram_id: int) -> None:
        self.storage.clear_admin_session(telegram_id)
        self.send(chat_id, "Не удалось найти редактируемый элемент. Действие сброшено.")
        self.show_main(chat_id)

    def render(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        message_id: int | None = None,
    ) -> None:
        if message_id is not None:
            try:
                self.api.edit_message_text(chat_id, message_id, text, reply_markup)
                return
            except TelegramError as error:
                if "message is not modified" in error.description.lower():
                    return
        self.send(chat_id, text, reply_markup)

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.api.send_message(chat_id, text, reply_markup)

    def safe_answer_callback(self, callback_id: str) -> None:
        try:
            self.api.answer_callback_query(callback_id)
        except TelegramError:
            pass

    def safe_answer_callback_async(self, callback_id: str) -> None:
        threading.Thread(
            target=self.safe_answer_callback,
            args=(callback_id,),
            daemon=True,
        ).start()


def parse_admin_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    result: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


def main() -> None:
    import os

    load_env(ROOT / ".env")
    token = os.getenv("ADMIN_BOT_TOKEN")
    if not token or token == "put_admin_bot_token_here":
        raise SystemExit("Укажите ADMIN_BOT_TOKEN в .env.")

    admin_ids = parse_admin_ids(os.getenv("ADMIN_TELEGRAM_IDS"))
    api = TelegramAPI(token)
    storage = Storage(DB_PATH)
    content = Content(CONTENT_PATH)
    with RuntimeLock(ROOT / "data" / "admin_bot.lock", "Админ-бот"):
        AdminBot(api, storage, content, admin_ids).run()
