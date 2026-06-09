from __future__ import annotations

import http.client
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
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

    def get_file_path(self, file_id: str) -> str:
        result = self.request("getFile", {"file_id": file_id})
        return result["file_path"]

    def download_file(self, file_path: str) -> bytes:
        url = f"{self.base_url.replace('/bot', '/file/bot', 1)}/{file_path}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except self._transient_errors() as error:
            raise TelegramError("downloadFile", f"temporary connection problem: {error}") from error

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

    def send_photo_file(
        self,
        chat_id: int,
        photo_path: Path,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "chat_id": str(chat_id),
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._multipart_request("sendPhoto", fields, "photo", photo_path)

    def _multipart_request(
        self,
        method: str,
        fields: dict[str, Any],
        file_field: str,
        file_path: Path,
    ) -> dict[str, Any]:
        boundary = f"----BotBoundary{uuid.uuid4().hex}"
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode()
        )
        body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())

        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
        except self._transient_errors() as error:
            raise TelegramError(method, f"temporary connection problem: {error}") from error
        if not result.get("ok"):
            raise TelegramError(method, result.get("description", "unknown error"))
        return result["result"]

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

    def edit_message_media_file(
        self,
        chat_id: int,
        message_id: int,
        photo_path: Path,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "media": json.dumps(
                {
                    "type": "photo",
                    "media": "attach://photo",
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                ensure_ascii=False,
            ),
        }
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._multipart_request("editMessageMedia", fields, "photo", photo_path)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.request("answerCallbackQuery", payload)
