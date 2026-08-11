"""Pure helpers for Telegram referral deep links."""

from __future__ import annotations

import re
from typing import Optional


REFERRAL_PAYLOAD = re.compile(r"^ref_([1-9][0-9]{0,19})$")


def parse_referral_payload(message_text: Optional[str]) -> Optional[int]:
    if not message_text:
        return None
    parts = message_text.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "/start":
        return None
    match = REFERRAL_PAYLOAD.fullmatch(parts[1])
    if match is None:
        return None
    return int(match.group(1))


def build_referral_link(bot_username: str, telegram_id: int) -> str:
    username = bot_username.strip().lstrip("@")
    if not username or not re.fullmatch(r"[A-Za-z0-9_]+", username):
        raise ValueError("Telegram bot username is invalid")
    return f"https://t.me/{username}?start=ref_{telegram_id}"
