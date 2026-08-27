# -*- coding: utf-8 -*-
"""Багрепорты в Telegram-бота (Bot API sendMessage)."""
import json
import platform
from pathlib import Path

import requests

from .deezer_client import load_config

def _load_release_info() -> dict:
    version_path = Path(__file__).resolve().parent.parent / "release" / "version.json"
    with version_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


_RELEASE_INFO = _load_release_info()
APP_VERSION = _RELEASE_INFO["version"]
APP_BUILD_ID = _RELEASE_INFO["build_id"]


def send_report(text: str, context: dict | None = None) -> dict:
    """Отправляет репорт в TG. Возвращает {"ok": bool, "error": str}."""
    cfg = load_config()
    tg = cfg.get("telegram", {})
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if not token or not chat_id:
        return {"ok": False, "error": "Telegram не настроен (нет bot_token/chat_id)"}

    lines = [
        f"DeckPipe bug report v{APP_VERSION} ({APP_BUILD_ID})",
        f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"wav_mode: {cfg.get('wav_mode', 'source')}",
    ]
    if context:
        if context.get("jobs_errors"):
            lines.append("Последние ошибки очереди:")
            for e in context["jobs_errors"][:5]:
                lines.append(f"  • {e}")
        if context.get("current"):
            lines.append(f"Экран: {context['current']}")
    lines += ["", "Сообщение пользователя:", text]

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines)[:4000]},
            timeout=20)
        if r.ok and r.json().get("ok"):
            return {"ok": True}
        return {"ok": False, "error": f"TG API: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
