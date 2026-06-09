from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


class Content:
    def __init__(self, path: Path, default_path: Path | None = None) -> None:
        self.path = path
        self._mtime: float | None = None
        if not self.path.exists():
            if default_path is None:
                raise FileNotFoundError(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(default_path, self.path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        self._mtime = self.path.stat().st_mtime
        return data

    def reload_if_changed(self) -> None:
        mtime = self.path.stat().st_mtime
        if self._mtime != mtime:
            self.data = self._load()

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._mtime = self.path.stat().st_mtime

    @property
    def intro(self) -> dict[str, str]:
        self.reload_if_changed()
        return self.data["intro"]

    @property
    def categories(self) -> list[dict[str, Any]]:
        self.reload_if_changed()
        return self.data["categories"]

    @property
    def messages(self) -> dict[str, str]:
        self.reload_if_changed()
        return self.data.setdefault("messages", {})

    @property
    def message_photos(self) -> dict[str, str]:
        self.reload_if_changed()
        return self.data.setdefault("message_photos", {})

    @property
    def directions(self) -> list[str]:
        self.reload_if_changed()
        return self.data.setdefault("directions", [])

    @property
    def interests(self) -> list[str]:
        self.reload_if_changed()
        return self.data.setdefault("interests", [])

    def message(self, key: str, default: str = "") -> str:
        return self.messages.get(key, default)

    def message_photo(self, key: str) -> str:
        return self.message_photos.get(key, "")

    def category(self, category_id: str) -> dict[str, Any] | None:
        return next((item for item in self.categories if item["id"] == category_id), None)

    def activity(self, category_id: str, activity_id: str) -> dict[str, Any] | None:
        category = self.category(category_id)
        if not category:
            return None
        return next((item for item in category["items"] if item["id"] == activity_id), None)
