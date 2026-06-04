from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class TelegramError(RuntimeError):
    def __init__(self, method: str, description: str) -> None:
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        payload = payload or {}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        request_timeout = self._request_timeout(method, payload)
        last_error: Exception | None = None

        max_attempts = self._max_attempts(method)
        for attempt in range(1, max_attempts + 1):
            started_at = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    raw = response.read().decode("utf-8")
                self._log_slow_request(method, started_at)
                break
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8")
                self._log_slow_request(method, started_at)
                break
            except self._transient_errors() as error:
                last_error = error
                if attempt == max_attempts:
                    raise TelegramError(method, f"temporary connection problem: {error}") from error
                time.sleep(0.4 * attempt)
        else:
            raise TelegramError(method, f"temporary connection problem: {last_error}")

        result = json.loads(raw)
        if not result.get("ok"):
            raise TelegramError(method, result.get("description", "unknown error"))
        return result.get("result")

    def _log_slow_request(self, method: str, started_at: float) -> None:
        elapsed = time.monotonic() - started_at
        if elapsed >= 2:
            print(f"slow Telegram request: {method} took {elapsed:.1f}s")

    def _request_timeout(self, method: str, payload: dict[str, Any]) -> int:
        if method == "getUpdates":
            return 6
        return 12

    def _max_attempts(self, method: str) -> int:
        if method == "getUpdates":
            return 1
        return 4

    def _transient_errors(self) -> tuple[type[BaseException], ...]:
        return (
            http.client.RemoteDisconnected,
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        )

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload)

    def get_me(self) -> dict[str, Any]:
        return self.request("getMe")

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload)

    def send_photo(
        self,
        chat_id: int,
        photo: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendPhoto", payload)

    def edit_message_media(
        self,
        chat_id: int,
        message_id: int,
        photo: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any] | bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "media": {
                "type": "photo",
                "media": photo,
                "caption": caption,
                "parse_mode": "HTML",
            },
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageMedia", payload)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.request("answerCallbackQuery", payload)
