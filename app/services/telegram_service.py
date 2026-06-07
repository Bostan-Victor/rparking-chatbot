from __future__ import annotations

import logging
import os

import requests

from ..models.lead import Lead

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"


_LANG_LABELS = {"ro": "Română", "en": "Engleză", "ru": "Rusă"}


def send_lead_notification(lead: Lead, lang: str = "ro") -> None:
    """Send a Telegram message with lead details to the configured chat.

    Raises ValueError if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set.
    Raises requests.RequestException on network/API failure.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is not set.")

    lang_label = _LANG_LABELS.get(lang, lang)
    text = (
        f"🅿️ Lead nou RParking:\n"
        f"Nume: {lead.name}\n"
        f"Companie: {lead.company or '—'}\n"
        f"Telefon: {lead.phone or '—'}\n"
        f"Email: {lead.email or '—'}\n"
        f"Nr. locuri parcare: {lead.nr_parking_spots or '—'}\n"
        f"Oraș: {lead.city or '—'}\n"
        f"Tip proiect: {lead.project_type or '—'}\n"
        f"Limbă preferată: {lang_label}"
    )

    response = requests.post(
        f"{_API_BASE}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    response.raise_for_status()


def send_transcript_document(*, lead_ref: str, messages: list[dict]) -> None:
    """Send the full conversation transcript as a .txt file to Telegram.

    Skips system messages. Labels turns as 'Client' / 'RParking'.
    Raises ValueError if env vars are missing.
    Raises requests.RequestException on failure.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is not set.")

    lines = [f"Transcript conversație — {lead_ref}", "=" * 44]
    for m in messages:
        role = m.get("role", "")
        if role == "system":
            continue
        label = "Client" if role == "user" else "RParking"
        lines.append(f"\n{label}:\n{m.get('content', '').strip()}")
    content = "\n".join(lines).encode("utf-8")

    response = requests.post(
        f"{_API_BASE}/bot{token}/sendDocument",
        data={"chat_id": chat_id, "caption": f"📋 Transcript — {lead_ref}"},
        files={"document": (f"transcript_{lead_ref}.txt", content, "text/plain")},
        timeout=15,
    )
    response.raise_for_status()


def send_manager_transfer_notification(*, name: str, phone: str, subject: str, lang: str = "ro") -> None:
    """Send a Telegram message when a user requests to speak with a manager.

    Raises ValueError if env vars are missing.
    Raises requests.RequestException on failure.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is not set.")

    lang_label = _LANG_LABELS.get(lang, lang)
    text = (
        f"🔔 Transfer Manager RParking:\n"
        f"Nume: {name or '—'}\n"
        f"Telefon: {phone or '—'}\n"
        f"Subiect: {subject or '—'}\n"
        f"Limbă preferată: {lang_label}"
    )

    response = requests.post(
        f"{_API_BASE}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    response.raise_for_status()


def send_reservation_notification(
    *, name: str, phone: str, email: str, project_type: str, reserved_datetime: str
) -> None:
    """Send a Telegram message when a demo reservation is created.

    Raises ValueError if env vars are missing.
    Raises requests.RequestException on failure.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is not set.")

    text = (
        f"📅 Rezervare Demo RParking:\n"
        f"Nume: {name or '—'}\n"
        f"Telefon: {phone or '—'}\n"
        f"Email: {email or '—'}\n"
        f"Tip proiect: {project_type or '—'}\n"
        f"Data și ora: {reserved_datetime or '—'}"
    )

    response = requests.post(
        f"{_API_BASE}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    response.raise_for_status()
