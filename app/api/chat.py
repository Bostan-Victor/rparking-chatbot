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
    _yes = {"da", "sigur", "ok", "bine", "desigur", "vreau", "doresc",  # RO
            "yes", "y", "sure", "yeah", "yep", "of course",              # EN
            "да", "конечно", "хочу", "ладно", "хорошо"}               # RU
    return any(p in words for p in _yes)


def _detect_no(text: str) -> bool:
    t = _normalize(text)
    words = t.split()
    if len(words) > 4:
        return False
    _no = {"nu", "nici", "nup",                                          # RO
           "no", "nope", "n", "not",                                     # EN
           "нет", "не", "нет спасибо"}                                  # RU
    return any(p in words for p in _no)


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
            "You are a STRICT classifier. "
            "Context: the RParking bot just asked the user if they want to schedule a demo "
            "(the question may be in Romanian, English, or Russian). "
            "Determine if the user's reply is EXPLICITLY POSITIVE (confirms they want a demo) "
            "or EXPLICITLY NEGATIVE (declines). "
            "If the message is a question, comment, or there is ANY doubt, reply UNKNOWN. "
            "Reply ONLY with: YES | NO | UNKNOWN. No other text."
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


_BUYING_INTENT_KEYWORDS = {
    "preț", "pret", "cost", "costă", "costa", "cât costă", "cat costa",
    "ofertă", "oferta", "buget", "investiție", "investitie",
    "instalare", "implementare", "achiziție", "achizitie",
    "cardpass", "card pass", "tichete", "qr code",
    "entry point", "exit point", "pay point",
    "câte locuri", "cate locuri", "locuri de parcare",
    "parcare nouă", "parcare noua", "sistem nou",
    "cum funcționează", "cum functioneaza",
    "cum se instalează", "cum se instaleaza",
    "vreau să", "vreau sa", "am nevoie",
    "ne interesează", "ne intereseaza",
    "pentru parcarea", "pentru parcare",
}


def _has_buying_intent(text: str, api_key: str, model: str) -> bool:
    """Returns True if the message shows buying/implementation interest."""
    t = _normalize(text)
    if any(kw in t for kw in _BUYING_INTENT_KEYWORDS):
        return True
    if not api_key:
        return False
    classifier = LLMClient(api_key=api_key, model=model)
    messages = [
        {"role": "system", "content": (
            "Ești un clasificator STRICT. "
            "Determină dacă mesajul utilizatorului arată interes real față de "
            "achiziționarea sau implementarea unui sistem de management al parcării "
            "(prețuri, instalare, produse specifice, dimensiunea parcării, cum funcționează). "
            "Răspunde DOAR cu: YES | NO. Fără text suplimentar."
        )},
        {"role": "user", "content": text},
    ]
    try:
        result = classifier.chat(messages=messages).strip().upper()
        return result == "YES"
    except Exception:
        return False


_LANG_QUICK: dict[str, set[str]] = {
    "en": {"what", "how", "is", "are", "for", "the", "and", "can", "do", "does",
           "your", "you", "parking", "system", "want", "have", "which", "where"},
}


def _detect_language(text: str, api_key: str, model: str) -> str:
    """Detect language from user text. Returns 'ro', 'en', or 'ru'. Defaults to 'ro'."""
    if re.search(r'[а-яА-ЯёЁ]', text):
        return "ru"
    words = set(_normalize(text).split())
    if len(words & _LANG_QUICK["en"]) >= 1:
        return "en"
    if not api_key:
        return "ro"
    classifier = LLMClient(api_key=api_key, model=model)
    messages = [
        {"role": "system", "content": (
            "Detect the language of the user's message. "
            "Reply with EXACTLY one token: ro | en | ru. No other text."
        )},
        {"role": "user", "content": text},
    ]
    try:
        result = classifier.chat(messages=messages).strip().lower()
        if result in {"ro", "en", "ru"}:
            return result
    except Exception:
        pass
    return "ro"


def _last_message_had_demo_offer(text: str | None) -> bool:
    """Returns True if the last assistant message contained a demo invitation (any language)."""
    if not text:
        return False
    t = _normalize(text)
    return (
        "demonstrație rparking" in t or "demonstratie rparking" in t
        or ("demonstrație" in t and "(da / nu)" in t)
        or ("rparking demo" in t and "(yes / no)" in t)
        or "демонстрацию rparking" in t
        or ("да / нет" in t and "rparking" in t)
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
        "- Fără introduceri de tipul «Înțeleg», «Sigur», «Desigur»\n\n"
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
               api_key: str, model: str, lang: str = "ro") -> str:
    kb_context = "\n\n".join(kb_snippets) if kb_snippets else ""
    system = _system_prompt()
    if kb_context:
        system += "\n\nBază de cunoștințe (RParking):\n" + kb_context
    system += f"\n\nIMPORTANT: Respond exclusively in {_LANG_NAMES[lang]}. Do not switch languages."

    messages = [{"role": "system", "content": system}] + [
        m for m in history if m.get("role") != "system"
    ]
    client = LLMClient(api_key=api_key, model=model)
    return _strip_leading_padding(client.chat(messages=messages))


# ---------------------------------------------------------------------------
# KB helper
# ---------------------------------------------------------------------------

_LANG_NAMES = {"ro": "română", "en": "English", "ru": "русский"}

_DEMO_OFFERS = {
    "ro": "\n\nDoriți să programați o demonstrație RParking? (da / nu)",
    "en": "\n\nWould you like to schedule an RParking demo? (yes / no)",
    "ru": "\n\nХотите записаться на демонстрацию RParking? (да / нет)",
}

_LC_STRINGS: dict[str, dict[str, str]] = {
    "ro": {
        "capture_start": "Super! Cum vă numiți, vă rog?",
        "name_err":       "Vă rog să îmi spuneți numele dvs. (minim 2 caractere).",
        "company_ask":    "Compania / Organizația dvs.?",
        "company_err":    "Vă rog să îmi spuneți numele companiei (minim 2 caractere).",
        "phone_ask":      "Numărul dvs. de telefon? (ex: 07xx xxx xxx / +40...)",
        "phone_err":      "Nu am recunoscut un număr valid. Vă rog să îl scrieți din nou (ex: 07xx xxx xxx / +40...).",
        "email_ask":      "Adresa de email? (ex: nume@companie.ro)",
        "email_err":      "Nu am recunoscut un email valid. Scrieți adresa (ex: nume@companie.ro) sau scrieți 'nu am'.",
        "email_skip":     "nu am,skip,fara,fără,sari",
        "nr_spots_ask":   "Câte locuri de parcare are proiectul dvs.? (număr aproximativ)",
        "city_ask":       "În ce oraş/localitate se află parcarea?",
        "city_err":       "Vă rog să îmi spuneți oraşul (minim 2 caractere).",
        "type_ask":       "Ce tip de sistem vă interesează? ({types})",
        "farewell":       (
            "Mulțumesc! Am înregistrat datele dvs.\n"
            "Un consultant RParking vă va contacta în cel mai scurt timp pentru a stabili detaliile demonstrației.\n\n"
            "Puteți folosi şi calendarul din interfață pentru a alege direct o dată şi oră disponibilă.\n\n"
            "Vă mulțumim că ați contactat RParking. O zi bună!"
        ),
        "save_err":       "A apărut o problemă la salvarea datelor. Puteți încerca din nou sau ne contactați direct.",
        "no_snippets":    "Nu am găsit informații relevante pentru întrebarea dvs. în baza noastră de date.",
        "ended":          "Această conversație s-a încheiat. Scrieți /new pentru a începe una nouă.",
    },
    "en": {
        "capture_start": "Great! What is your name, please?",
        "name_err":       "Please enter your name (at least 2 characters).",
        "company_ask":    "Your company / organization?",
        "company_err":    "Please enter the company name (at least 2 characters).",
        "phone_ask":      "Your phone number? (e.g. +40 7xx xxx xxx)",
        "phone_err":      "I couldn't recognize a valid number. Please try again (e.g. +40 7xx xxx xxx).",
        "email_ask":      "Your email address? (e.g. name@company.com)",
        "email_err":      "I couldn't recognize a valid email. Enter the address or type 'skip'.",
        "email_skip":     "skip,no email,none,i don't have",
        "nr_spots_ask":   "Approximately how many parking spots does your project have?",
        "city_ask":       "In which city / location is the parking?",
        "city_err":       "Please enter the city name (at least 2 characters).",
        "type_ask":       "Which type of system are you interested in? ({types})",
        "farewell":       (
            "Thank you! We have recorded your details.\n"
            "An RParking consultant will contact you shortly to schedule the demonstration.\n\n"
            "You can also use the calendar in the interface to pick a date and time directly.\n\n"
            "Thank you for contacting RParking. Have a great day!"
        ),
        "save_err":       "There was a problem saving your data. Please try again or contact us directly.",
        "no_snippets":    "I couldn't find relevant information for your question in our knowledge base.",
        "ended":          "This conversation has ended. Type /new to start a new one.",
    },
    "ru": {
        "capture_start": "Отлично! Как вас зовут?",
        "name_err":       "Пожалуйста, введите ваше имя (минимум 2 символа).",
        "company_ask":    "Ваша компания / организация?",
        "company_err":    "Пожалуйста, введите название компании (минимум 2 символа).",
        "phone_ask":      "Ваш номер телефона? (например: +40 7xx xxx xxx)",
        "phone_err":      "Не удалось распознать номер. Попробуйте ещё раз (например: +40 7xx xxx xxx).",
        "email_ask":      "Ваш адрес электронной почты? (например: name@company.com)",
        "email_err":      "Не удалось распознать email. Введите адрес или напишите 'нет'.",
        "email_skip":     "нет,пропустить,skip,no email",
        "nr_spots_ask":   "Примерное количество парковочных мест в вашем проекте?",
        "city_ask":       "В каком городе / населённом пункте находится парковка?",
        "city_err":       "Пожалуйста, введите название города (минимум 2 символа).",
        "type_ask":       "Какой тип системы вас интересует? ({types})",
        "farewell":       (
            "Спасибо! Ваши данные записаны.\n"
            "Консультант RParking свяжется с вами в ближайшее время.\n\n"
            "Вы также можете воспользоваться календарём в интерфейсе, чтобы выбрать время напрямую.\n\n"
            "Благодарим за обращение в RParking. Хорошего дня!"
        ),
        "save_err":       "Произошла ошибка при сохранении данных. Попробуйте ещё раз или свяжитесь с нами напрямую.",
        "no_snippets":    "Я не нашёл релевантной информации по вашему вопросу в нашей базе знаний.",
        "ended":          "Этот разговор завершён. Введите /new, чтобы начать новый.",
    },
}


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
# Lead capture flow handler
# ---------------------------------------------------------------------------

def _handle_lead_capture(conversation_id: str, user_text: str, meta: dict, lang: str = "ro") -> str | None:
    """Handle multi-step lead contact collection for RParking demo requests."""
    lead_state = meta.get("lead") or {}
    if not lead_state.get("active"):
        return None

    s = _LC_STRINGS[lang]
    step = lead_state.get("step")
    draft = dict(lead_state.get("draft") or {})

    if step == "name":
        name = user_text.strip()
        if len(name) < 2:
            return s["name_err"]
        draft["name"] = name
        lead_state.update({"step": "company", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return s["company_ask"]

    if step == "company":
        company = user_text.strip()
        if len(company) < 2:
            return s["company_err"]
        draft["company"] = company
        lead_state.update({"step": "phone", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return s["phone_ask"]

    if step == "phone":
        if not is_valid_phone(user_text.strip()):
            return s["phone_err"]
        draft["phone"] = user_text.strip()
        lead_state.update({"step": "email", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return s["email_ask"]

    if step == "email":
        t = _normalize(user_text)
        skip_keys = [k.strip() for k in s["email_skip"].split(",")]
        if any(k in t for k in skip_keys):
            draft["email"] = None
        else:
            email = _extract_email(user_text)
            if not email or not is_valid_email(email):
                return s["email_err"]
            draft["email"] = email
        lead_state.update({"step": "nr_spots", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return s["nr_spots_ask"]

    if step == "nr_spots":
        m = re.search(r"\d+", user_text)
        draft["nr_parking_spots"] = int(m.group(0)) if m else None
        lead_state.update({"step": "city", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return s["city_ask"]

    if step == "city":
        city = user_text.strip()
        if len(city) < 2:
            return s["city_err"]
        draft["city"] = city
        types_str = " / ".join(_PROJECT_TYPES)
        lead_state.update({"step": "project_type", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return s["type_ask"].format(types=types_str)

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
            return s["save_err"]

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

        return s["farewell"]

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
            "demo_offered": False,
            "lang": "ro",
            "lead_lang": None,
            "lead": {"active": False, "step": None, "draft": {}},
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
    api_key = current_app.config.get("OPENAI_API_KEY", "")
    model = current_app.config.get("OPENAI_MODEL", "gpt-4o-mini")

    # Conversation closed — ignore further messages
    if stage == "ended":
        ended_lang = meta.get("lead_lang") or meta.get("lang", "ro")
        return jsonify({
            "conversation_id": conversation_id,
            "reply": _LC_STRINGS[ended_lang]["ended"],
        })

    def _respond(reply: str) -> object:
        _store.append(conversation_id, {"role": "assistant", "content": reply})
        return jsonify({"conversation_id": conversation_id, "reply": reply})

    # ── Detect language (locked during lead capture, live during chatting) ────
    if stage == "lead_capture":
        lang = meta.get("lead_lang") or "ro"
    else:
        lang = _detect_language(user_text, api_key, model)
        _store.update_meta(conversation_id, {"lang": lang})

    # ── Active lead capture flow (highest priority) ───────────────────────────
    lead_reply = _handle_lead_capture(conversation_id, user_text, meta, lang)
    if lead_reply is not None:
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(lead_reply)

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
                "lead_lang": lang,
            })
            _store.append(conversation_id, {"role": "user", "content": user_text})
            return _respond(_LC_STRINGS[lang]["capture_start"])
        # NO or UNKNOWN → fall through to answer normally

    # KB + LLM answer
    try:
        snippets = _kb_search(user_text)
    except ValueError as exc:
        return jsonify({"error": "config_error", "message": str(exc)}), 500

    _store.append(conversation_id, {"role": "user", "content": user_text})
    history = _store.get(conversation_id)

    if not snippets:
        reply = _LC_STRINGS[lang]["no_snippets"]
    else:
        try:
            reply = _llm_reply(
                user_text=user_text, history=history, kb_snippets=snippets,
                api_key=api_key, model=model, lang=lang,
            )
        except ValueError as exc:
            return jsonify({"error": "config_error", "message": str(exc)}), 500

    for marker in [o.strip() for o in _DEMO_OFFERS.values()]:
        if marker in reply:
            reply = reply[:reply.index(marker)].rstrip()
            break

    if not meta.get("demo_offered") and _has_buying_intent(user_text, api_key, model):
        _store.update_meta(conversation_id, {"demo_offered": True})
        return _respond(reply.rstrip() + _DEMO_OFFERS[lang])
    return _respond(reply.rstrip())
