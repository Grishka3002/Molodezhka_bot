from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_LOCK = threading.Lock()


@dataclass
class Student:
    telegram_id: int
    full_name: str | None = None
    phone: str | None = None
    age: int | None = None
    gender: str | None = None
    direction: str | None = None
    interests: list[str] | None = None
    state: str | None = None
    last_bot_message_id: int | None = None
    last_bot_message_kind: str | None = None

    @property
    def is_complete(self) -> bool:
        return bool(
            self.full_name
            and self.phone
            and self.age
            and self.gender
            and self.direction
            and self.interests
        )

    @property
    def first_name(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        if not self.full_name:
            return "студент"
        return self.full_name.strip()


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        with SCHEMA_LOCK:
            self._init_schema()

    def _init_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT,
                phone TEXT,
                age INTEGER,
                gender TEXT,
                direction TEXT,
                interests TEXT NOT NULL DEFAULT '[]',
                state TEXT,
                last_bot_message_id INTEGER,
                last_bot_message_kind TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._add_column_if_missing("students", "gender", "TEXT")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_sessions (
                telegram_id INTEGER PRIMARY KEY,
                state TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_signups (
                telegram_id INTEGER NOT NULL,
                category_id TEXT NOT NULL,
                activity_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, category_id, activity_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_offsets (
                bot_name TEXT PRIMARY KEY,
                next_offset INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            try:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():
                    raise

    def get_or_create_student(self, telegram_id: int) -> Student:
        row = self.connection.execute(
            "SELECT * FROM students WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO students (telegram_id) VALUES (?)",
                (telegram_id,),
            )
            self.connection.commit()
            return Student(telegram_id=telegram_id, interests=[])
        return self._row_to_student(row)

    def update_student(self, telegram_id: int, **fields: Any) -> Student:
        if not fields:
            return self.get_or_create_student(telegram_id)
        allowed = {
            "full_name",
            "phone",
            "age",
            "gender",
            "direction",
            "interests",
            "state",
            "last_bot_message_id",
            "last_bot_message_kind",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown student fields: {', '.join(sorted(unknown))}")

        normalized = dict(fields)
        if "interests" in normalized:
            normalized["interests"] = json.dumps(normalized["interests"], ensure_ascii=False)

        assignments = ", ".join(f"{field} = ?" for field in normalized)
        values = list(normalized.values())
        values.append(telegram_id)
        self.connection.execute(
            f"""
            UPDATE students
            SET {assignments}, updated_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
            """,
            values,
        )
        self.connection.commit()
        return self.get_or_create_student(telegram_id)

    def reset_profile(self, telegram_id: int) -> Student:
        self.connection.execute(
            """
            UPDATE students
            SET full_name = NULL,
                phone = NULL,
                age = NULL,
                gender = NULL,
                direction = NULL,
                interests = '[]',
                state = 'ask_full_name',
                updated_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        self.connection.commit()
        return self.get_or_create_student(telegram_id)

    def add_activity_signup(self, telegram_id: int, category_id: str, activity_id: str) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO activity_signups (telegram_id, category_id, activity_id)
            VALUES (?, ?, ?)
            """,
            (telegram_id, category_id, activity_id),
        )
        self.connection.commit()

    def has_activity_signup(self, telegram_id: int, category_id: str, activity_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM activity_signups
            WHERE telegram_id = ? AND category_id = ? AND activity_id = ?
            """,
            (telegram_id, category_id, activity_id),
        ).fetchone()
        return row is not None

    def activity_signup_rows(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT s.telegram_id,
                   s.gender,
                   s.direction,
                   s.interests,
                   a.category_id,
                   a.activity_id
            FROM activity_signups a
            JOIN students s ON s.telegram_id = a.telegram_id
            """
        ).fetchall()
        return [
            {
                "telegram_id": row["telegram_id"],
                "gender": row["gender"],
                "direction": row["direction"],
                "interests": json.loads(row["interests"] or "[]"),
                "category_id": row["category_id"],
                "activity_id": row["activity_id"],
            }
            for row in rows
        ]

    def get_bot_offset(self, bot_name: str) -> int | None:
        row = self.connection.execute(
            "SELECT next_offset FROM bot_offsets WHERE bot_name = ?",
            (bot_name,),
        ).fetchone()
        if row is None:
            return None
        return row["next_offset"]

    def set_bot_offset(self, bot_name: str, next_offset: int) -> None:
        self.connection.execute(
            """
            INSERT INTO bot_offsets (bot_name, next_offset)
            VALUES (?, ?)
            ON CONFLICT(bot_name) DO UPDATE SET
                next_offset = excluded.next_offset,
                updated_at = CURRENT_TIMESTAMP
            """,
            (bot_name, next_offset),
        )
        self.connection.commit()

    def get_admin_session(self, telegram_id: int) -> tuple[str | None, dict[str, Any]]:
        row = self.connection.execute(
            "SELECT state, payload FROM admin_sessions WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is None:
            return None, {}
        return row["state"], json.loads(row["payload"] or "{}")

    def set_admin_session(self, telegram_id: int, state: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO admin_sessions (telegram_id, state, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                state = excluded.state,
                payload = excluded.payload,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_id, state, json.dumps(payload, ensure_ascii=False)),
        )
        self.connection.commit()

    def clear_admin_session(self, telegram_id: int) -> None:
        self.connection.execute(
            "DELETE FROM admin_sessions WHERE telegram_id = ?",
            (telegram_id,),
        )
        self.connection.commit()

    def _row_to_student(self, row: sqlite3.Row) -> Student:
        return Student(
            telegram_id=row["telegram_id"],
            full_name=row["full_name"],
            phone=row["phone"],
            age=row["age"],
            gender=row["gender"],
            direction=row["direction"],
            interests=json.loads(row["interests"] or "[]"),
            state=row["state"],
            last_bot_message_id=row["last_bot_message_id"],
            last_bot_message_kind=row["last_bot_message_kind"],
        )
