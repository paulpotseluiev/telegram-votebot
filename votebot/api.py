"""Тонка обгортка Bot API на requests.

Свідомо без python-telegram-bot: нам потрібно рівно вісім методів, а крон-режим
не потребує ані асинхронності, ані диспетчера апдейтів.
"""
import logging
import time
from pathlib import Path

import requests

from .config import BOT_TOKEN

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

# Що саме нам потрібно з черги. message_reaction прилітає лише якщо бот —
# адміністратор чату І цей тип явно вказано тут.
ALLOWED_UPDATES = ["message", "edited_message", "message_reaction", "message_reaction_count"]


class TelegramError(RuntimeError):
    """Telegram відповів ok=false і повторювати немає сенсу."""


def _flatten(params: dict) -> dict:
    """Для multipart значення мають бути рядками, а None — зникнути."""
    out = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        out[key] = "true" if value is True else "false" if value is False else value
    return out


class Bot:
    def __init__(self, token: str | None = None, timeout: int = 60):
        self.token = (token or BOT_TOKEN).strip()
        if not self.token:
            raise SystemExit("Не задано BOT_TOKEN (.env або змінна оточення)")
        self.timeout = timeout
        self.session = requests.Session()

    # --- Транспорт ---------------------------------------------------------

    def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        file_field: str | None = None,
        file_path: Path | None = None,
        retries: int = 4,
        read_timeout: int | None = None,
    ):
        url = f"{API_ROOT}/bot{self.token}/{method}"
        timeout = read_timeout or self.timeout
        delay = 1.0
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                if file_path:
                    # Файл відкриваємо всередині циклу: після невдалої спроби
                    # хендл уже вичерпано і повтор відправив би нуль байтів.
                    with open(file_path, "rb") as handle:
                        response = self.session.post(
                            url, data=_flatten(params), files={file_field: handle}, timeout=timeout
                        )
                else:
                    response = self.session.post(url, json=_flatten(params), timeout=timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("%s: мережева помилка (спроба %s/%s): %s", method, attempt, retries, exc)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            try:
                data = response.json()
            except ValueError:
                last_error = f"HTTP {response.status_code}, не JSON"
                log.warning("%s: %s (спроба %s/%s)", method, last_error, attempt, retries)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if data.get("ok"):
                return data["result"]

            code = data.get("error_code")
            description = data.get("description", "")

            if code == 429:
                wait = int(data.get("parameters", {}).get("retry_after", 5))
                log.warning("%s: flood wait %s с", method, wait)
                time.sleep(wait + 1)
                continue

            if code and 500 <= code < 600:
                last_error = description
                log.warning("%s: %s %s (спроба %s/%s)", method, code, description, attempt, retries)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            raise TelegramError(f"{method}: {description} (код {code})")

        raise TelegramError(f"{method}: не вдалося після {retries} спроб ({last_error})")

    # --- Методи ------------------------------------------------------------

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_chat(self, chat_id: int) -> dict:
        return self.call("getChat", {"chat_id": chat_id})

    def get_chat_member(self, chat_id: int, user_id: int) -> dict:
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def get_updates(self, offset: int, *, limit: int = 100, timeout: int = 0) -> list[dict]:
        return self.call(
            "getUpdates",
            {"offset": offset, "limit": limit, "timeout": timeout, "allowed_updates": ALLOWED_UPDATES},
            read_timeout=self.timeout + timeout,
        )

    def send_media(self, chat_id: int, path: Path, kind: str, caption: str | None = None) -> dict:
        """kind ∈ {mp4, gif, png}. mp4/gif ідуть як animation — Telegram їх зациклює."""
        if kind == "png":
            method, field = "sendPhoto", "photo"
        else:
            method, field = "sendAnimation", "animation"
        return self.call(
            method,
            {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            file_field=field,
            file_path=Path(path),
        )

    def send_message(self, chat_id: int, text: str, *, disable_notification: bool = False) -> dict:
        return self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": disable_notification,
                "link_preview_options": {"is_disabled": True},
            },
        )

    def edit_message_caption(self, chat_id: int, message_id: int, caption: str) -> dict:
        return self.call(
            "editMessageCaption",
            {"chat_id": chat_id, "message_id": message_id, "caption": caption, "parse_mode": "HTML"},
        )

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            return self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError as exc:
            # Уже видалене руками повідомлення — не привід валити раунд
            log.warning("Не вдалося видалити %s у %s: %s", message_id, chat_id, exc)
            return False

    def download_file(self, file_id: str, dest: Path) -> Path:
        info = self.call("getFile", {"file_id": file_id})
        remote = info["file_path"]
        url = f"{API_ROOT}/file/bot{self.token}/{remote}"

        dest = Path(dest)
        suffix = Path(remote).suffix or ".jpg"
        dest = dest.with_suffix(suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)

        with self.session.get(url, stream=True, timeout=self.timeout) as response:
            response.raise_for_status()
            with open(dest, "wb") as handle:
                for chunk in response.iter_content(65536):
                    handle.write(chunk)
        return dest
