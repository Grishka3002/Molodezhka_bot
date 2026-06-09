from __future__ import annotations

import html
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .content import Content
from .db import Storage, Student
from .runtime_lock import RuntimeLock
from .telegram_api import TelegramAPI, TelegramError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT_PATH = ROOT / "content" / "activities.json"
CONTENT_PATH = ROOT / "data" / "activities.json"
DB_PATH = ROOT / "data" / "bot.sqlite3"


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def clean(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


class YouthBot:
    def __init__(
        self,
        api: TelegramAPI,
        storage: Storage,
        content: Content,
        bot_name: str = "student",
    ) -> None:
        self.api = api
        self.storage = storage
        self.content = content
        self.bot_name = bot_name

    def run(self) -> None:
        print("Bot started. Press Ctrl+C to stop.")
        offset = self.start_offset()
        error_count = 0
        while True:
            try:
                updates = self.api.get_updates(offset=offset)
                if error_count:
                    print("Telegram connection restored.")
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
                        print(f"Student bot failed on update {update.get('update_id')}:")
                        traceback.print_exc()
            except TelegramError as error:
                error_count += 1
                delay = min(60, 3 * error_count)
                if error_count == 1 or error_count % 5 == 0:
                    print(f"Telegram connection problem, retry in {delay}s: {error}")
                time.sleep(delay)
            except KeyboardInterrupt:
                print("Bot stopped.")
                return

    def start_offset(self) -> int | None:
        offset = self.storage.get_bot_offset(self.bot_name)
        if offset is not None:
            return offset

        try:
            pending = self.api.get_updates(offset=None)
        except TelegramError as error:
            print(f"Bot could not check old pending updates at startup: {error}")
            return None

        if not pending:
            return None

        offset = max(update["update_id"] for update in pending) + 1
        self.storage.set_bot_offset(self.bot_name, offset)
        print(f"Bot skipped {len(pending)} old pending updates.")
        return offset

    def log_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            text = (update["message"].get("text") or "").strip()
            print(f"Student update {update['update_id']}: message {text[:40]!r}")
        elif "callback_query" in update:
            data = update["callback_query"].get("data") or ""
            print(f"Student update {update['update_id']}: callback {data[:40]!r}")

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        telegram_id = message["from"]["id"]
        text = (message.get("text") or "").strip()
        student = self.storage.get_or_create_student(telegram_id)

        if text.startswith("/start"):
            student = self.prepare_fresh_command_screen(chat_id, student)
            self.start(chat_id, student)
            return
        if text.startswith("/menu"):
            student = self.prepare_fresh_command_screen(chat_id, student)
            self.show_main_menu(chat_id, student)
            return
        if text.startswith("/profile"):
            student = self.prepare_fresh_command_screen(chat_id, student)
            self.show_profile(chat_id, student)
            return
        if text.startswith("/restart"):
            student = self.prepare_fresh_command_screen(chat_id, student)
            student = self.storage.reset_profile(telegram_id)
            self.ask_current_profile_question(chat_id, student)
            return

        if student.state:
            self.process_profile_answer(chat_id, message, student, text)
            return

        self.show_main_menu(chat_id, student)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        data = callback.get("data") or ""
        message = callback["message"]
        chat_id = message["chat"]["id"]
        telegram_id = callback["from"]["id"]
        student = self.storage.get_or_create_student(telegram_id)
        message_kind = "photo" if message.get("photo") else "text"
        student.last_bot_message_id = message["message_id"]
        student.last_bot_message_kind = message_kind

        self.safe_answer_callback_async(callback_id)

        if data.startswith("gender:"):
            self.choose_gender(chat_id, student, data.split(":", 1)[1])
        elif data.startswith("interest:"):
            self.toggle_interest(chat_id, student, int(data.split(":", 1)[1]))
        elif data == "interests_done":
            self.finish_interests(chat_id, student)
        elif data.startswith("direction:"):
            self.choose_direction(chat_id, student, int(data.split(":", 1)[1]))
        elif data.startswith("signup:"):
            _, category_id, activity_id = data.split(":", 2)
            self.signup_for_activity(chat_id, student, category_id, activity_id)
        elif data == "direction_manual":
            student = self.storage.update_student(student.telegram_id, state="ask_direction_manual")
            self.render_screen(
                chat_id,
                student,
                "Напишите направление обучения текстом:",
                None,
                photo=self.content.message_photo("ask_direction"),
            )
        elif data == "main":
            self.show_main_menu(chat_id, student)
        elif data == "categories":
            self.show_categories(chat_id, student)
        elif data in {"recommendations", "popular"}:
            self.show_recommendations(chat_id, student)
        elif data == "profile":
            self.show_profile(chat_id, student)
        elif data == "restart_profile":
            student = self.storage.reset_profile(telegram_id)
            self.ask_current_profile_question(chat_id, student)
        elif data.startswith("category:"):
            self.show_category(chat_id, student, data.split(":", 1)[1])
        elif data.startswith("activity:"):
            _, category_id, activity_id = data.split(":", 2)
            self.show_activity(chat_id, student, category_id, activity_id)
        else:
            self.render_screen(
                chat_id,
                student,
                self.content.message("unknown_section", "Не нашел такой раздел. Вернемся в главное меню."),
                inline_keyboard([[("В главное меню", "main")]]),
                self.content.message_photo("unknown_section"),
            )

    def start(self, chat_id: int, student: Student) -> None:
        if student.is_complete:
            self.show_main_menu(chat_id, student)
            return
        student = self.storage.update_student(
            student.telegram_id,
            state=student.state or self.next_profile_state(student),
        )
        self.ask_current_profile_question(chat_id, student)

    def next_profile_state(self, student: Student) -> str:
        if not student.full_name:
            return "ask_full_name"
        if not student.phone:
            return "ask_phone"
        if not student.age:
            return "ask_age"
        if not student.gender:
            return "ask_gender"
        if not student.interests:
            return "ask_interests"
        if not student.direction:
            return "ask_direction"
        return "ask_full_name"

    def process_profile_answer(
        self,
        chat_id: int,
        message: dict[str, Any],
        student: Student,
        text: str,
    ) -> None:
        if student.state == "ask_full_name":
            if len(text.split()) < 2:
                self.render_screen(
                    chat_id,
                    student,
                    self.content.message("invalid_full_name", "Введите ФИО полностью, например: Иванов Иван Иванович."),
                    None,
                    photo=self.content.message_photo("invalid_full_name"),
                )
                return
            student = self.storage.update_student(
                student.telegram_id,
                full_name=text,
                state="ask_phone",
            )
        elif student.state == "ask_phone":
            if len([char for char in text if char.isdigit()]) < 10:
                self.render_screen(
                    chat_id,
                    student,
                    self.content.message("invalid_phone", "Введите контактный номер, например: +7 999 123-45-67."),
                    None,
                    photo=self.content.message_photo("invalid_phone"),
                )
                return
            student = self.storage.update_student(
                student.telegram_id,
                phone=text,
                state="ask_age",
            )
        elif student.state == "ask_age":
            try:
                age = int(text)
            except ValueError:
                self.render_screen(
                    chat_id,
                    student,
                    self.content.message("invalid_age_format", "Введите возраст числом, например: 18."),
                    None,
                    photo=self.content.message_photo("invalid_age_format"),
                )
                return
            if age < 14 or age > 80:
                self.render_screen(
                    chat_id,
                    student,
                    self.content.message("invalid_age_range", "Проверьте возраст: нужно число от 14 до 80."),
                    None,
                    photo=self.content.message_photo("invalid_age_range"),
                )
                return
            student = self.storage.update_student(
                student.telegram_id,
                age=age,
                state="ask_gender",
            )
        elif student.state in {"ask_gender", "ask_interests"}:
            self.ask_current_profile_question(chat_id, student)
        elif student.state in {"ask_direction", "ask_direction_manual"}:
            if len(text) < 3:
                self.render_screen(
                    chat_id,
                    student,
                    self.content.message("invalid_direction", "Напишите направление обучения чуть подробнее."),
                    None,
                    photo=self.content.message_photo("invalid_direction"),
                )
                return
            student = self.storage.update_student(
                student.telegram_id,
                direction=text,
                state=None,
            )
            self.render_screen(
                chat_id,
                student,
                clean(
                    self.content.message(
                        "profile_complete",
                        "Анкета готова, {name}.\n\nТеперь выберите, что вам интересно, и бот покажет подходящие объединения.",
                    ).replace("{name}", student.first_name)
                ),
                inline_keyboard([[("Смотреть активности", "categories")], [("Мой профиль", "profile")]]),
                self.content.message_photo("profile_complete"),
            )
            self.send_profile_recommendations(chat_id, student)
            return

        self.ask_current_profile_question(chat_id, student)

    def choose_gender(self, chat_id: int, student: Student, gender: str) -> None:
        if student.state != "ask_gender":
            self.show_main_menu(chat_id, student)
            return
        labels = {
            "male": "Мужской",
            "female": "Женский",
            "not_specified": "Не указывать",
        }
        if gender not in labels:
            self.ask_current_profile_question(chat_id, student)
            return
        student = self.storage.update_student(
            student.telegram_id,
            gender=labels[gender],
            interests=[],
            state="ask_interests",
        )
        self.ask_current_profile_question(chat_id, student)

    def toggle_interest(self, chat_id: int, student: Student, interest_index: int) -> None:
        if student.state != "ask_interests":
            self.show_main_menu(chat_id, student)
            return
        interests = self.content.interests
        if interest_index < 0 or interest_index >= len(interests):
            self.ask_current_profile_question(chat_id, student)
            return
        selected = student.interests or []
        interest = interests[interest_index]
        if interest in selected:
            selected.remove(interest)
        else:
            selected.append(interest)
        student = self.storage.update_student(student.telegram_id, interests=selected)
        self.ask_current_profile_question(chat_id, student)

    def finish_interests(self, chat_id: int, student: Student) -> None:
        if student.state != "ask_interests":
            self.show_main_menu(chat_id, student)
            return
        if not student.interests:
            self.render_screen(
                chat_id,
                student,
                self.content.message("invalid_interests", "Выберите хотя бы один интерес."),
                self.interests_keyboard(student),
                self.content.message_photo("invalid_interests"),
            )
            return
        student = self.storage.update_student(student.telegram_id, state="ask_direction")
        self.ask_current_profile_question(chat_id, student)

    def choose_direction(self, chat_id: int, student: Student, direction_index: int) -> None:
        directions = self.content.directions
        if student.state not in {"ask_direction", "ask_direction_manual"}:
            self.show_main_menu(chat_id, student)
            return
        if direction_index < 0 or direction_index >= len(directions):
            self.ask_current_profile_question(chat_id, student)
            return
        student = self.storage.update_student(
            student.telegram_id,
            direction=directions[direction_index],
            state=None,
        )
        self.render_screen(
            chat_id,
            student,
            clean(
                self.content.message(
                    "profile_complete",
                    "Анкета готова, {name}.\n\nТеперь выберите, что вам интересно, и бот покажет подходящие объединения.",
                ).replace("{name}", student.first_name)
            ),
            inline_keyboard([[("Смотреть активности", "categories")], [("Мой профиль", "profile")]]),
            self.content.message_photo("profile_complete"),
        )
        self.send_profile_recommendations(chat_id, student)

    def ask_current_profile_question(self, chat_id: int, student: Student) -> None:
        questions = {
            "ask_full_name": self.content.message("ask_full_name", "Для начала заполните короткую анкету.\n\nВведите ваше ФИО:"),
            "ask_phone": self.content.message("ask_phone", "Введите контактный номер телефона:"),
            "ask_age": self.content.message("ask_age", "Введите возраст:"),
            "ask_gender": self.content.message("ask_gender", "Выберите ваш пол:"),
            "ask_interests": self.content.message("ask_interests", "Выберите интересы. Можно выбрать несколько вариантов, затем нажмите «Готово»:"),
            "ask_direction": self.content.message("ask_direction", "Введите направление, на котором вы учитесь:"),
            "ask_direction_manual": "Напишите направление обучения текстом:",
        }
        text = questions.get(student.state or "", "Давайте начнем заново. Введите ваше ФИО:")
        keyboard = None
        if student.state == "ask_gender":
            keyboard = inline_keyboard(
                [
                    [("Мужской", "gender:male"), ("Женский", "gender:female")],
                    [("Не указывать", "gender:not_specified")],
                ]
            )
        elif student.state == "ask_interests":
            keyboard = self.interests_keyboard(student)
        elif student.state == "ask_direction" and self.content.directions:
            rows = [[(direction, f"direction:{index}")] for index, direction in enumerate(self.content.directions)]
            rows.append([("Ввести вручную", "direction_manual")])
            keyboard = inline_keyboard(rows)
        self.render_screen(chat_id, student, text, keyboard, self.content.message_photo(student.state or ""))

    def interests_keyboard(self, student: Student) -> dict[str, Any]:
        selected = set(student.interests or [])
        rows: list[list[tuple[str, str]]] = []
        for index, interest in enumerate(self.content.interests):
            prefix = "[x] " if interest in selected else "[ ] "
            rows.append([(f"{prefix}{interest}", f"interest:{index}")])
        rows.append([("Готово", "interests_done")])
        return inline_keyboard(rows)

    def show_main_menu(self, chat_id: int, student: Student) -> None:
        if not student.is_complete:
            self.start(chat_id, student)
            return
        intro = self.content.intro
        text = (
            f"{clean(intro['title'])} приветствует вас, {clean(student.first_name)}.\n\n"
            f"{clean(intro['text'])}\n\n"
            f"{clean(self.content.message('main_choose', 'Выберите раздел:'))}"
        )
        self.render_screen(
            chat_id,
            student,
            text,
            inline_keyboard(
                [
                    [("Активности", "categories")],
                    [("Рекомендации", "recommendations")],
                    [("Мой профиль", "profile")],
                    [("Заполнить анкету заново", "restart_profile")],
                ]
            ),
            intro.get("photo", ""),
        )

    def show_categories(self, chat_id: int, student: Student) -> None:
        rows = [[(category["title"], f"category:{category['id']}")] for category in self.content.categories]
        rows.append([("В главное меню", "main")])
        text = self.content.message(
            "categories_intro",
            "Что вам интересно?\n\nВыберите направление, а дальше бот покажет доступные клубы и контакты.",
        )
        self.render_screen(chat_id, student, text, inline_keyboard(rows), self.content.message_photo("categories_intro"))

    def show_category(self, chat_id: int, student: Student, category_id: str) -> None:
        category = self.content.category(category_id)
        if not category:
            self.show_categories(chat_id, student)
            return

        rows = [
            [(item["title"], f"activity:{category_id}:{item['id']}")]
            for item in category["items"]
            if self.activity_allowed(student, item)
        ]
        rows.append([("Назад к активностям", "categories")])
        rows.append([("В главное меню", "main")])
        text = (
            f"<b>{clean(category['title'])}</b>\n\n"
            f"{clean(category['description'])}\n\n"
            f"{clean(self.content.message('category_choose', 'Выберите конкретное объединение:'))}"
        )
        self.render_screen(chat_id, student, text, inline_keyboard(rows), category.get("photo", ""))

    def show_activity(
        self,
        chat_id: int,
        student: Student,
        category_id: str,
        activity_id: str,
    ) -> None:
        activity = self.content.activity(category_id, activity_id)
        category = self.content.category(category_id)
        if not activity or not category or not self.activity_allowed(student, activity):
            self.show_categories(chat_id, student)
            return

        signed_up = self.storage.has_activity_signup(student.telegram_id, category_id, activity_id)
        text = (
            f"<b>{clean(activity['title'])}</b>\n\n"
            f"{clean(activity['description'])}\n\n"
            f"<b>Где и когда:</b>\n{clean(activity.get('where'))}"
        )
        rows = []
        if signed_up:
            text = f"{text}\n\n<b>Контакты для записи:</b>\n{clean(activity.get('contact'))}"
        else:
            text = f"{text}\n\nЧтобы открыть контакты, нажмите «Записаться»."
            rows.append([("Записаться", f"signup:{category_id}:{activity_id}")])
        rows.extend(
            [
                [("Назад к разделу", f"category:{category_id}")],
                [("Все активности", "categories")],
                [("В главное меню", "main")],
            ]
        )
        keyboard = inline_keyboard(rows)
        photo = (activity.get("photo") or "").strip()
        self.render_screen(chat_id, student, text, keyboard, photo)

    def signup_for_activity(
        self,
        chat_id: int,
        student: Student,
        category_id: str,
        activity_id: str,
    ) -> None:
        activity = self.content.activity(category_id, activity_id)
        if not activity or not self.activity_allowed(student, activity):
            self.show_categories(chat_id, student)
            return
        self.storage.add_activity_signup(student.telegram_id, category_id, activity_id)
        self.show_activity(chat_id, student, category_id, activity_id)

    def send_profile_recommendations(self, chat_id: int, student: Student) -> None:
        recommendations = self.recommended_activities(student, limit=5)
        if not recommendations:
            self.api.send_message(
                chat_id,
                self.content.message(
                    "recommendations_empty",
                    "Пока не получилось собрать персональную подборку. Можно посмотреть все активности и выбрать то, что понравится.",
                ),
                inline_keyboard([[("Все активности", "categories")], [("В главное меню", "main")]]),
            )
            return
        rows = [
            [(title, f"activity:{category_id}:{activity_id}")]
            for category_id, activity_id, title in recommendations
        ]
        rows.append([("Все активности", "categories")])
        rows.append([("В главное меню", "main")])
        text = clean(
            self.content.message(
                "recommendations_after_profile",
                "Приятно познакомиться, {name}!\n\nНа основе вашей анкеты мы подобрали несколько объединений, которые могут вам подойти:",
            ).replace("{name}", student.first_name)
        )
        self.api.send_message(chat_id, text, inline_keyboard(rows))

    def show_recommendations(self, chat_id: int, student: Student) -> None:
        recommendations = self.recommended_activities(student, limit=5)
        if not recommendations:
            self.render_screen(
                chat_id,
                student,
                self.content.message(
                    "recommendations_empty",
                    "Пока не получилось собрать персональную подборку. Можно посмотреть все активности и выбрать то, что понравится.",
                ),
                inline_keyboard([[("Все активности", "categories")], [("В главное меню", "main")]]),
                self.content.message_photo("recommendations_empty"),
            )
            return
        rows = [[(title, f"activity:{category_id}:{activity_id}")] for category_id, activity_id, title in recommendations]
        rows.append([("Все активности", "categories")])
        rows.append([("В главное меню", "main")])
        text = self.content.message(
            "recommendations_intro",
            "Рекомендации для вас:\n\nПодборка составлена на основе вашей анкеты и записей студентов с похожими интересами.",
        )
        self.render_screen(chat_id, student, text, inline_keyboard(rows), self.content.message_photo("recommendations_intro"))

    def show_popular(self, chat_id: int, student: Student) -> None:
        popular = self.popular_activities(student, limit=5)
        if not popular:
            self.render_screen(
                chat_id,
                student,
                self.content.message(
                    "popular_empty",
                    "Пока недостаточно записей для персональной подборки. Можно посмотреть все активности и записаться первым.",
                ),
                inline_keyboard([[("Все активности", "categories")], [("В главное меню", "main")]]),
                self.content.message_photo("popular_empty"),
            )
            return
        rows = [[(title, f"activity:{category_id}:{activity_id}")] for category_id, activity_id, title in popular]
        rows.append([("Все активности", "categories")])
        rows.append([("В главное меню", "main")])
        text = self.content.message(
            "popular_intro",
            "Популярное среди похожих студентов:\n\nПодборка считается по тем, кто уже нажимал «Записаться».",
        )
        self.render_screen(chat_id, student, text, inline_keyboard(rows), self.content.message_photo("popular_intro"))

    def recommended_activities(self, student: Student, limit: int) -> list[tuple[str, str, str]]:
        return self.popular_activities(student, limit)

    def popular_activities(self, student: Student, limit: int) -> list[tuple[str, str, str]]:
        activities = self.activity_lookup(student)
        scores: dict[tuple[str, str], int] = {}
        student_interests = set(student.interests or [])
        for row in self.storage.activity_signup_rows():
            key = (row["category_id"], row["activity_id"])
            if key not in activities:
                continue
            score = 1
            if student.direction and row.get("direction") == student.direction:
                score += 3
            if student.gender and row.get("gender") == student.gender:
                score += 2
            score += len(student_interests & set(row.get("interests") or []))
            scores[key] = scores.get(key, 0) + score

        if not scores:
            return self.fallback_popular_activities(student, limit)

        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [
            (category_id, activity_id, activities[(category_id, activity_id)]["title"])
            for (category_id, activity_id), _score in ordered[:limit]
        ]

    def fallback_popular_activities(self, student: Student, limit: int) -> list[tuple[str, str, str]]:
        student_interests = [item.lower() for item in (student.interests or [])]
        matches: list[tuple[str, str, str]] = []
        all_items: list[tuple[str, str, str]] = []
        for category in self.content.categories:
            for activity in category.get("items", []):
                if not self.activity_allowed(student, activity):
                    continue
                entry = (category["id"], activity["id"], activity["title"])
                all_items.append(entry)
                haystack = " ".join(
                    [
                        category.get("title", ""),
                        category.get("description", ""),
                        activity.get("title", ""),
                        activity.get("description", ""),
                    ]
                ).lower()
                if any(interest in haystack for interest in student_interests):
                    matches.append(entry)
        return (matches or all_items)[:limit]

    def activity_lookup(self, student: Student | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (category["id"], activity["id"]): activity
            for category in self.content.categories
            for activity in category.get("items", [])
            if student is None or self.activity_allowed(student, activity)
        }

    def activity_allowed(self, student: Student, activity: dict[str, Any]) -> bool:
        audience = activity.get("audience", "mixed")
        if audience == "mixed" or not student.gender:
            return True
        if student.gender == "Мужской":
            return audience != "female"
        if student.gender == "Женский":
            return audience != "male"
        return True

    def show_profile(self, chat_id: int, student: Student) -> None:
        interests = ", ".join(student.interests or []) or "пока не выбрано"
        text = (
            "<b>Ваша анкета</b>\n\n"
            f"ФИО: {clean(student.full_name)}\n"
            f"Телефон: {clean(student.phone)}\n"
            f"Возраст: {clean(str(student.age) if student.age else '')}\n"
            f"Пол: {clean(student.gender)}\n"
            f"Направление: {clean(student.direction)}\n"
            f"Интересы: {clean(interests)}"
        )
        self.render_screen(
            chat_id,
            student,
            text,
            inline_keyboard(
                [
                    [("Активности", "categories")],
                    [("Рекомендации", "recommendations")],
                    [("Заполнить анкету заново", "restart_profile")],
                    [("В главное меню", "main")],
                ]
            ),
            self.content.message_photo("profile"),
        )

    def render_screen(
        self,
        chat_id: int,
        student: Student,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        photo: str | None = None,
    ) -> None:
        photo = (photo or "").strip()
        if photo:
            try:
                self.render_photo(chat_id, student, photo, text, reply_markup)
                return
            except (TelegramError, OSError) as error:
                print(f"Photo could not be shown, using text screen instead: {error}")
        self.render_text(chat_id, student, text, reply_markup)

    def prepare_fresh_command_screen(self, chat_id: int, student: Student) -> Student:
        if student.last_bot_message_id:
            self.try_delete_previous_async(chat_id, student)
        return self.storage.update_student(
            student.telegram_id,
            last_bot_message_id=None,
            last_bot_message_kind=None,
        )

    def render_text(
        self,
        chat_id: int,
        student: Student,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if student.last_bot_message_id and student.last_bot_message_kind == "text":
            try:
                self.api.edit_message_text(chat_id, student.last_bot_message_id, text, reply_markup)
                return
            except TelegramError as error:
                if "message is not modified" in error.description.lower():
                    return

        message = self.api.send_message(chat_id, text, reply_markup)
        self.try_delete_previous_async(chat_id, student)
        self.storage.update_student(
            student.telegram_id,
            last_bot_message_id=message["message_id"],
            last_bot_message_kind="text",
        )

    def send_plain_text(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        photo: str | None = None,
    ) -> None:
        photo = (photo or "").strip()
        if photo:
            try:
                local_photo = self.local_photo_path(photo)
                if local_photo:
                    self.api.send_photo_file(chat_id, local_photo, text, reply_markup)
                else:
                    self.api.send_photo(chat_id, photo, text, reply_markup)
                return
            except (TelegramError, OSError) as error:
                print(f"Photo could not be sent, using text message instead: {error}")
        self.api.send_message(chat_id, text, reply_markup)

    def render_photo(
        self,
        chat_id: int,
        student: Student,
        photo: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        local_photo = self.local_photo_path(photo)
        if local_photo:
            if student.last_bot_message_id and student.last_bot_message_kind == "photo":
                try:
                    self.api.edit_message_media_file(
                        chat_id,
                        student.last_bot_message_id,
                        local_photo,
                        caption,
                        reply_markup,
                    )
                    return
                except TelegramError as error:
                    if "message is not modified" in error.description.lower():
                        return
            message = self.api.send_photo_file(chat_id, local_photo, caption, reply_markup)
            self.try_delete_previous_async(chat_id, student)
            self.storage.update_student(
                student.telegram_id,
                last_bot_message_id=message["message_id"],
                last_bot_message_kind="photo",
            )
            return

        if student.last_bot_message_id and student.last_bot_message_kind == "photo":
            try:
                self.api.edit_message_media(
                    chat_id,
                    student.last_bot_message_id,
                    photo,
                    caption,
                    reply_markup,
                )
                return
            except TelegramError as error:
                if "message is not modified" in error.description.lower():
                    return

        message = self.api.send_photo(chat_id, photo, caption, reply_markup)
        self.try_delete_previous_async(chat_id, student)
        self.storage.update_student(
            student.telegram_id,
            last_bot_message_id=message["message_id"],
            last_bot_message_kind="photo",
        )

    def local_photo_path(self, photo: str) -> Path | None:
        path = Path(photo)
        if not path.is_absolute():
            path = ROOT / path
        return path if path.is_file() else None

    def try_delete_previous(self, chat_id: int, student: Student) -> None:
        if student.last_bot_message_id:
            self.try_delete(chat_id, student.last_bot_message_id)

    def try_delete_previous_async(self, chat_id: int, student: Student) -> None:
        if not student.last_bot_message_id:
            return
        threading.Thread(
            target=self.try_delete,
            args=(chat_id, student.last_bot_message_id),
            daemon=True,
        ).start()

    def try_delete(self, chat_id: int, message_id: int) -> None:
        try:
            self.api.delete_message(chat_id, message_id)
        except TelegramError:
            pass

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


def main() -> None:
    load_env(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "put_your_token_here":
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN в .env или переменных окружения.")

    api = TelegramAPI(token)
    storage = Storage(DB_PATH)
    content = Content(CONTENT_PATH, DEFAULT_CONTENT_PATH)
    with RuntimeLock(ROOT / "data" / "student_bot.lock", "Основной бот"):
        YouthBot(api, storage, content).run()
