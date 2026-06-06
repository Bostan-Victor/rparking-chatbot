from __future__ import annotations

import os
import re

from flask import Blueprint, current_app, jsonify, request

from ..services.conversation_store import InMemoryConversationStore
from ..services.knowledge_base import KnowledgeBase
from ..services.llm_client import LLMClient
from ..services.lead_service import LeadService
from ..services.telegram_service import (
    send_lead_notification,
    send_manager_transfer_notification,
    send_transcript_document,
)
from ..services.validators import is_valid_email, is_valid_phone

# ---------------------------------------------------------------------------
# RParking chatbot — flow: open → chatting → lead_capture → ended
# Demo offer appended only when buying intent is detected (once per conversation).
# ---------------------------------------------------------------------------


bp = Blueprint("chat", __name__)

_store = InMemoryConversationStore(max_messages=30)


_PROJECT_TYPES = [
    "Sistem complet",
    "Sistem CardPass",
    "Sistem cu tichete",
    "Sistem QR Code",
    "Altul",
]


# ---------------------------------------------------------------------------
# Project type helpers
# ---------------------------------------------------------------------------

_PROJECT_TYPE_KEYWORDS: dict[str, set[str]] = {
    "Sistem complet": {"complet", "integral", "toate", "tot", "full"},
    "Sistem CardPass": {"cardpass", "card pass", "card", "rfid", "nfc", "abonament"},
    "Sistem cu tichete": {"tichet", "tichete", "ticket", "bilet"},
    "Sistem QR Code": {"qr", "qrcode", "qr code", "cod qr"},
}


def _extract_project_type(text: str) -> str | None:
    t = _normalize(text)
    for label, keywords in _PROJECT_TYPE_KEYWORDS.items():
        for k in keywords:
            if k in t:
                return label
    if "altul" in t or "alt" in t or "alta" in t or "altceva" in t:
        return "Altul"
    return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _detect_yes(text: str) -> bool:
    t = _normalize(text)
    words = t.split()
    if len(words) > 4:
        return False
    return any(p in words for p in ["da", "sigur", "ok", "bine", "desigur", "vreau", "doresc"]) or t in {"yes", "y"}


def _detect_no(text: str) -> bool:
    t = _normalize(text)
    words = t.split()
    if len(words) > 4:
        return False
    return any(p in words for p in ["nu", "nici", "nup", "no"]) or t in {"n"}


def _classify_yes_no(text: str, api_key: str, model: str) -> str:
    """LLM-based yes/no classifier. Returns 'YES', 'NO', or 'UNKNOWN'."""
    if _detect_yes(text):
        return "YES"
    if _detect_no(text):
        return "NO"
    if not api_key:
        return "UNKNOWN"
    classifier = LLMClient(api_key=api_key, model=model)
    messages = [
        {"role": "system", "content": (
            "Ești un clasificator STRICT. "
            "Contextul: botul RParking tocmai a întrebat utilizatorul: 'Doriți să programați o demonstrație RParking? (da / nu)'. "
            "Determină dacă mesajul utilizatorului este EXPLICIT un răspuns POZITIV (confirmă că vrea demo) "
            "sau EXPLICIT NEGATIV (refuză). "
            "Dacă mesajul este o întrebare, un comentariu sau există ORICE dubiu, răspunde cu UNKNOWN. "
            "Răspunde DOAR cu: YES | NO | UNKNOWN. Fără text suplimentar."
        )},
        {"role": "user", "content": text},
    ]
    try:
        result = classifier.chat(messages=messages).strip().upper()
        if result in {"YES", "NO", "UNKNOWN"}:
            return result
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _last_message_had_demo_offer(text: str | None) -> bool:
    """Returns True if the last assistant message ended with a demo invitation."""
    if not text:
        return False
    t = _normalize(text)
    return "demonstrație rparking" in t or "demonstratie rparking" in t or (
        "demonstrație" in t and "(da / nu)" in t
    )


def _extract_email(text: str) -> str | None:
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return m.group(0) if m else None


def _last_assistant_message(history: list[dict]) -> str | None:
    for m in reversed(history):
        if m.get("role") == "assistant":
            return m.get("content")
    return None


def _strip_leading_padding(text: str) -> str:
    if not isinstance(text, str):
        return text
    out = text.lstrip()
    for _ in range(2):
        new_out = re.sub(
            r"^(Înțeleg|Inteleg|Sigur|Desigur|Bine|Perfect|Ok|Okay|În regulă|In regulă)\s*[\.!,:;\-–]\s+",
            "", out, flags=re.IGNORECASE,
        )
        if new_out == out:
            break
        out = new_out.lstrip()
    return out


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _system_prompt() -> str:
    return (
        "Ești un asistent virtual pentru RParking, o companie din România care oferă soluții complete "
        "de management al parcărilor: Entry Point, Exit Point, Pay Point, BackOffice Software, "
        "integrare NFC, QR Code, Card Access, emitere tichete, plăți numerar și card.\n\n"
        "Scopul tău:\n"
        "- Răspunzi la întrebările despre produsele și soluțiile RParking\n"
        "- Explici funcționalitățile sistemelor de parcare\n"
        "- Orientezi clientul spre soluția potrivită\n\n"
        "Stil:\n"
        "- Profesional, clar, concis\n"
        "- Răspunsuri scurte, 2–4 propoziții\n"
        "- Fără introduceri de tipul «Înțeleg», «Sigur», «Desigur»\n"
        "- Vorbești mereu în română\n\n"
        "Dacă informația nu este în baza de cunoștințe, spui că nu poți răspunde la acel subiect.\n\n"
        "IMPORTANT: Nu menționa niciodată demonstrații, demo-uri sau programări în răspunsurile tale. "
        "Nu întreba utilizatorul dacă dorește un demo sau să fie contactat. "
        "Răspunde DOAR la întrebarea pusă, fără niciun call-to-action la final."
    )


def _greeting() -> str:
    return (
        "Bună! Sunt asistentul virtual RParking.\n"
        "Vă pot ajuta cu informații despre soluțiile noastre de management al parcărilor.\n\n"
        "Cu ce vă pot ajuta astăzi?"
    )


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _llm_reply(*, user_text: str, history: list[dict], kb_snippets: list[str],
               api_key: str, model: str) -> str:
    kb_context = "\n\n".join(kb_snippets) if kb_snippets else ""
    system = _system_prompt()
    if kb_context:
        system += "\n\nBază de cunoștințe (RParking):\n" + kb_context

    messages = [{"role": "system", "content": system}] + [
        m for m in history if m.get("role") != "system"
    ]
    client = LLMClient(api_key=api_key, model=model)
    return _strip_leading_padding(client.chat(messages=messages))


# ---------------------------------------------------------------------------
# KB helper
# ---------------------------------------------------------------------------

_DEMO_OFFER = "\n\nDoriți să programați o demonstrație RParking? (da / nu)"


def _kb_search(user_text: str) -> list[str]:
    project_root = os.path.abspath(os.path.join(current_app.root_path, os.pardir))
    kb = KnowledgeBase(
        kb_dir=os.path.join(project_root, "kb"),
        index_path=os.path.join(project_root, "kb_index.json"),
        openai_api_key=current_app.config.get("OPENAI_API_KEY", ""),
        embedding_model=current_app.config.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    return kb.search(user_text)


# ---------------------------------------------------------------------------
# Manager transfer intent + flow
# ---------------------------------------------------------------------------

_MANAGER_KEYWORDS = {
    "manager", "sef", "șef", "operator", "uman", "human",
    "persoana", "persoană", "persoane", "vorbesc cu", "sa vorbesc",
    "să vorbesc", "angajat", "contact direct", "suna-ma", "sunați-mă",
    "sunati-ma", "responsabil", "reprezentant", "agent",
}


def _detect_manager_intent(text: str, api_key: str, model: str) -> bool:
    """Returns True if user wants to speak with a human manager/operator."""
    t = _normalize(text)
    if any(kw in t for kw in _MANAGER_KEYWORDS):
        return True
    if not api_key:
        return False
    classifier = LLMClient(api_key=api_key, model=model)
    messages = [
        {"role": "system", "content": (
            "You are a STRICT classifier. "
            "Determine if the user's message expresses a desire to speak with a human "
            "manager, operator, or agent (in any language — Romanian, English, Russian). "
            "Reply ONLY with: YES | NO. No other text."
        )},
        {"role": "user", "content": text},
    ]
    try:
        result = classifier.chat(messages=messages).strip().upper()
        return result == "YES"
    except Exception:
        return False


def _handle_manager_transfer(conversation_id: str, user_text: str, meta: dict) -> str | None:
    """Handle 3-step manager transfer collection: name → phone → subject."""
    mt_state = meta.get("manager_transfer") or {}
    if not mt_state.get("active"):
        return None

    step = mt_state.get("step")
    draft = dict(mt_state.get("draft") or {})

    if step == "name":
        name = user_text.strip()
        if len(name) < 2:
            return "Vă rog să îmi spuneți numele dvs. (minim 2 caractere)."
        draft["name"] = name
        mt_state.update({"step": "phone", "draft": draft})
        _store.update_meta(conversation_id, {"manager_transfer": mt_state})
        return "Numărul dvs. de telefon? (ex: 07xx xxx xxx / +40...)"

    if step == "phone":
        if not is_valid_phone(user_text.strip()):
            return "Nu am recunoscut un număr valid. Vă rog să îl scrieți din nou (ex: 07xx xxx xxx / +40...)."
        draft["phone"] = user_text.strip()
        mt_state.update({"step": "subject", "draft": draft})
        _store.update_meta(conversation_id, {"manager_transfer": mt_state})
        return "Cu ce subiect doriți să vorbiți cu un manager RParking?"

    if step == "subject":
        subject = user_text.strip()
        if len(subject) < 3:
            return "Vă rog să descrieți pe scurt subiectul (minim 3 caractere)."
        draft["subject"] = subject
        mt_state.update({"step": None, "active": False, "draft": draft})
        _store.update_meta(conversation_id, {"manager_transfer": mt_state, "stage": "ended"})

        try:
            send_manager_transfer_notification(
                name=draft.get("name", ""),
                phone=draft.get("phone", ""),
                subject=subject,
            )
        except Exception as exc:
            current_app.logger.warning("Manager transfer notification failed: %s", exc)

        try:
            history = _store.get(conversation_id)
            send_transcript_document(
                lead_ref=f"Manager Transfer — {draft.get('name', 'necunoscut')}",
                messages=history,
            )
        except Exception as exc:
            current_app.logger.warning("Manager transfer transcript failed: %s", exc)

        return (
            "Mulțumesc! Datele dvs. au fost transmise.\n"
            "Un manager RParking vă va contacta în cel mai scurt timp.\n\n"
            "Vă mulțumim că ați contactat RParking. O zi bună!"
        )

    return None


# ---------------------------------------------------------------------------
# Lead capture flow handler
# ---------------------------------------------------------------------------

def _handle_lead_capture(conversation_id: str, user_text: str, meta: dict) -> str | None:
    """Handle multi-step lead contact collection for RParking demo requests."""
    lead_state = meta.get("lead") or {}
    if not lead_state.get("active"):
        return None

    step = lead_state.get("step")
    draft = dict(lead_state.get("draft") or {})

    if step == "name":
        name = user_text.strip()
        if len(name) < 2:
            return "Vă rog să îmi spuneți numele dvs. (minim 2 caractere)."
        draft["name"] = name
        lead_state.update({"step": "company", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return "Compania / Organizația dvs.?"

    if step == "company":
        company = user_text.strip()
        if len(company) < 2:
            return "Vă rog să îmi spuneți numele companiei (minim 2 caractere)."
        draft["company"] = company
        lead_state.update({"step": "phone", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return "Numărul dvs. de telefon? (ex: 07xx xxx xxx / +40...)"

    if step == "phone":
        if not is_valid_phone(user_text.strip()):
            return "Nu am recunoscut un număr valid. Vă rog să îl scrieți din nou (ex: 07xx xxx xxx / +40...)."
        draft["phone"] = user_text.strip()
        lead_state.update({"step": "email", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return "Adresa de email? (ex: nume@companie.ro)"

    if step == "email":
        t = _normalize(user_text)
        if any(k in t for k in ["nu am", "skip", "fara", "fără", "sari"]):
            draft["email"] = None
        else:
            email = _extract_email(user_text)
            if not email or not is_valid_email(email):
                return "Nu am recunoscut un email valid. Scrieți adresa (ex: nume@companie.ro) sau scrieți 'nu am'."
            draft["email"] = email
        lead_state.update({"step": "nr_spots", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return "Câte locuri de parcare are proiectul dvs.? (număr aproximativ)"

    if step == "nr_spots":
        m = re.search(r"\d+", user_text)
        draft["nr_parking_spots"] = int(m.group(0)) if m else None
        lead_state.update({"step": "city", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return "În ce oraș/localitate se află parcarea?"

    if step == "city":
        city = user_text.strip()
        if len(city) < 2:
            return "Vă rog să îmi spuneți orașul (minim 2 caractere)."
        draft["city"] = city
        types_str = " / ".join(_PROJECT_TYPES)
        lead_state.update({"step": "project_type", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return f"Ce tip de sistem vă interesează? ({types_str})"

    if step == "project_type":
        pt = _extract_project_type(user_text)
        if not pt:
            pt = user_text.strip() if len(user_text.strip()) >= 2 else "Altul"
        draft["project_type"] = pt
        lead_state.update({"step": None, "active": False, "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state, "stage": "ended"})

        payload = {
            "name": draft.get("name"),
            "company": draft.get("company"),
            "phone": draft.get("phone"),
            "email": draft.get("email"),
            "nr_parking_spots": draft.get("nr_parking_spots"),
            "city": draft.get("city"),
            "project_type": pt,
        }

        try:
            lead = LeadService.create_lead(payload)
        except Exception as exc:
            current_app.logger.warning("Lead creation failed: %s", exc)
            _store.update_meta(conversation_id, {"stage": "lead_capture", "lead": {"active": True, "step": "project_type", "draft": draft}})
            return (
                "A apărut o problemă la salvarea datelor. "
                "Puteți încerca din nou sau ne contactați direct."
            )

        try:
            send_lead_notification(lead)
        except Exception as exc:
            current_app.logger.warning("Telegram lead notification failed: %s", exc)

        try:
            history = _store.get(conversation_id)
            send_transcript_document(
                lead_ref=f"Lead #{lead.id} — {draft.get('company', 'necunoscut')}",
                messages=history,
            )
        except Exception as exc:
            current_app.logger.warning("Lead transcript send failed: %s", exc)

        return (
            "Mulțumesc! Am înregistrat datele dvs.\n"
            "Un consultant RParking vă va contacta în cel mai scurt timp pentru a stabili detaliile demonstrației.\n\n"
            "Puteți folosi și calendarul din interfață pentru a alege direct o dată și oră disponibilă.\n\n"
            "Vă mulțumim că ați contactat RParking. O zi bună!"
        )

    return None


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@bp.post("/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    conversation_id = payload.get("conversation_id")
    message = payload.get("message")

    # First call: create conversation and return greeting
    if not conversation_id:
        greeting = _greeting()
        conversation_id = _store.create(initial_messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "assistant", "content": greeting},
        ])
        _store.update_meta(conversation_id, {
            "stage": "chatting",
            "lead": {"active": False, "step": None, "draft": {}},
            "manager_transfer": {"active": False, "step": None, "draft": {}},
        })
        return jsonify({"conversation_id": conversation_id, "reply": greeting})

    if not _store.exists(conversation_id):
        return jsonify({
            "error": "unknown_conversation",
            "message": "Unknown conversation_id. Start a new conversation without conversation_id.",
        }), 400

    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "invalid_request", "message": "'message' is required."}), 400

    user_text = message.strip()
    meta = _store.get_meta(conversation_id)
    stage = meta.get("stage")

    # Conversation closed — ignore further messages
    if stage == "ended":
        return jsonify({
            "conversation_id": conversation_id,
            "reply": "Această conversație s-a încheiat. Scrieți /new pentru a începe una nouă.",
        })

    api_key = current_app.config.get("OPENAI_API_KEY", "")
    model = current_app.config.get("OPENAI_MODEL", "gpt-4o-mini")

    def _respond(reply: str) -> object:
        _store.append(conversation_id, {"role": "assistant", "content": reply})
        return jsonify({"conversation_id": conversation_id, "reply": reply})

    # ── Active lead capture flow (highest priority) ───────────────────────────
    lead_reply = _handle_lead_capture(conversation_id, user_text, meta)
    if lead_reply is not None:
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(lead_reply)

    # ── Active manager transfer flow ──────────────────────────────────────────
    mt_reply = _handle_manager_transfer(conversation_id, user_text, meta)
    if mt_reply is not None:
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(mt_reply)

    # ── Manager intent check (only when no lead capture is active) ────────────
    if _detect_manager_intent(user_text, api_key, model):
        _store.update_meta(conversation_id, {
            "manager_transfer": {"active": True, "step": "name", "draft": {}},
        })
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(
            "Înțeles! Vă voi pune în legătură cu un manager RParking.\n"
            "Cum vă numiți, vă rog?"
        )

    # ── Stage: chatting — KB + LLM, demo offer after every reply ─────────────
    history = _store.get(conversation_id)
    last_assistant = _last_assistant_message(history)

    # Check if user is responding to a demo offer
    if _last_message_had_demo_offer(last_assistant):
        yn = _classify_yes_no(user_text, api_key, model)
        if yn == "YES":
            _store.update_meta(conversation_id, {
                "lead": {"active": True, "step": "name", "draft": {}},
                "stage": "lead_capture",
            })
            _store.append(conversation_id, {"role": "user", "content": user_text})
            return _respond("Super! Cum vă numiți, vă rog?")
        if yn == "NO":
            _store.append(conversation_id, {"role": "user", "content": user_text})
            return _respond("Bine, vă pot ajuta cu altceva?")
        # UNKNOWN → user ignored offer, fall through to answer normally

    # KB + LLM answer
    try:
        snippets = _kb_search(user_text)
    except ValueError as exc:
        return jsonify({"error": "config_error", "message": str(exc)}), 500

    _store.append(conversation_id, {"role": "user", "content": user_text})
    history = _store.get(conversation_id)

    if not snippets:
        reply = "Nu am găsit informații relevante pentru întrebarea dvs. în baza noastră de date."
    else:
        try:
            reply = _llm_reply(
                user_text=user_text, history=history, kb_snippets=snippets,
                api_key=api_key, model=model,
            )
        except ValueError as exc:
            return jsonify({"error": "config_error", "message": str(exc)}), 500

    demo_marker = _DEMO_OFFER.strip()
    if demo_marker in reply:
        reply = reply[:reply.index(demo_marker)].rstrip()

    if snippets:
        return _respond(reply.rstrip() + _DEMO_OFFER)
    return _respond(reply.rstrip())
