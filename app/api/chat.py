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

_PROJECT_TYPES_DISPLAY: dict[str, list[str]] = {
    "ro": ["Sistem complet", "Sistem CardPass", "Sistem cu tichete", "Sistem QR Code", "Altul"],
    "en": ["Complete System", "CardPass System", "Ticket System", "QR Code System", "Other"],
    "ru": ["Полная система", "Система CardPass", "Система с талонами", "Система QR Code", "Другое"],
}

_PROJECT_TYPE_KEYWORDS: dict[str, set[str]] = {
    "Sistem complet": {"complet", "integral", "toate", "tot", "full", "complete", "полная", "полный"},
    "Sistem CardPass": {"cardpass", "card pass", "card", "rfid", "nfc", "abonament", "карта", "карточка"},
    "Sistem cu tichete": {"tichet", "tichete", "ticket", "bilet", "ticketing", "талон", "талоны"},
    "Sistem QR Code": {"qr", "qrcode", "qr code", "cod qr", "куар"},
}


def _extract_project_type(text: str) -> str | None:
    t = _normalize(text)
    for label, keywords in _PROJECT_TYPE_KEYWORDS.items():
        for k in keywords:
            if k in t:
                return label
    if any(k in t for k in ["altul", "alt", "alta", "altceva", "other", "else", "другое", "иное", "другой"]):
        return "Altul"
    return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_EN_STOPWORDS = {
    "the", "is", "are", "how", "what", "do", "can", "i", "you",
    "please", "need", "want", "my", "have", "does", "it",
    "this", "that", "a", "an", "and", "or", "for", "of", "with",
    "me", "about", "tell", "show", "get", "give", "let", "more",
    "some", "any", "all", "at", "in", "on", "by", "we", "they",
    "he", "she", "was", "will", "would", "has", "had", "been",
    "be", "not", "but", "so", "if", "as", "up", "out",
    "your", "our", "their", "its", "his", "her", "when", "where",
    "which", "who", "than", "then", "there", "here", "into",
    "like", "just", "know", "use", "work", "make", "see", "also",
}
_RO_DIACRITICS = "ăâîșțĂÂÎȘȚ"


def _detect_language(text: str) -> str:
    """Detect language from text. Returns 'ro', 'en', or 'ru'. Default: 'ro'."""
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    words = set(_normalize(text).split())
    if words & _EN_STOPWORDS and not any(c in text for c in _RO_DIACRITICS):
        return "en"
    return "ro"


_YES_WORDS = {
    "da", "sigur", "ok", "bine", "desigur", "vreau", "doresc",
    "haida", "hai", "merge", "mergem", "perfect", "absolut",
    "evident", "neaparat", "super", "excelent", "gata",
    "de acord", "in regula", "în regulă", "cu placere", "cu plăcere",
    "fire", "lasam", "lăsăm", "incercam", "încercăm",
    # English
    "sure", "yes", "yeah", "yep", "absolutely", "great", "okay",
    # Russian
    "да", "конечно", "хочу", "хорошо", "давай", "ладно", "окей",
}
_YES_PHRASES = {
    "sa incercam", "să încercăm", "sa mergem", "să mergem",
    "de ce nu", "de ce nu?", "hai sa", "hai să",
    "vreau sa incerc", "vreau să încerc",
    "sounds good", "let's go", "why not", "of course", "i'd like that",
    "почему нет", "давайте", "с удовольствием", "хочу попробовать",
}


def _detect_yes(text: str) -> bool:
    t = _normalize(text)
    if t in _YES_PHRASES or any(t.startswith(p) for p in _YES_PHRASES):
        return True
    words = t.split()
    if len(words) > 5:
        return False
    return any(p in words for p in _YES_WORDS) or t in {"yes", "y"}


def _detect_no(text: str) -> bool:
    t = _normalize(text)
    words = t.split()
    if len(words) > 4:
        return False
    return (
        any(p in words for p in ["nu", "nici", "nup", "no", "not", "нет"])
        or t in {"n", "nope", "нет", "не хочу", "не надо"}
    )


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
            "Ești un clasificator. "
            "Contextul: botul RParking tocmai a întrebat utilizatorul: 'Doriți să programați o demonstrație RParking? (da / nu)'. "
            "Determină dacă mesajul utilizatorului acceptă (pozitiv) sau refuză (negativ) demonstrația. "
            "Acceptă atât răspunsuri EXPLICITE cât și IMPLICITE pozitive — "
            "ex: 'da', 'sigur', 'sa incercam', 'de ce nu', 'hai', 'merge', 'sounds good', 'почему нет' = YES. "
            "Răspunsuri negative: 'nu', 'no', 'nu vreau', 'nu acum', 'lasă' = NO. "
            "Returnează UNKNOWN doar dacă mesajul este clar o întrebare despre sistem sau complet off-topic. "
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
    """Returns True if the last assistant message ended with a demo invitation (any language)."""
    if not text:
        return False
    t = _normalize(text)
    return "(da / nu)" in t or "(yes / no)" in t or "(да / нет)" in t


_DEMO_REQUEST_KEYWORDS = {
    "vreau demo", "doresc demo", "am nevoie de demo",
    "vreau demonstratie", "doresc demonstratie", "vreau o demonstratie",
    "doresc o demonstratie", "vreau demonstrație", "doresc demonstrație",
    "vreau o demonstrație", "doresc o demonstrație",
    "programez o demonstratie", "rezerv o demonstratie",
    "programare demo", "rezervare demo",
    "schedule demo", "book a demo", "request a demo", "i want a demo",
    "хочу демо", "хочу демонстрацию", "запишите на демо",
}


def _detect_demo_request_intent(text: str, api_key: str, model: str) -> bool:
    """Returns True if user explicitly wants to schedule a demo (not just ask about it)."""
    t = _normalize(text)
    if any(kw in t for kw in _DEMO_REQUEST_KEYWORDS):
        return True
    if not api_key:
        return False
    classifier = LLMClient(api_key=api_key, model=model)
    messages = [
        {"role": "system", "content": (
            "You are a STRICT classifier. "
            "Context: RParking parking management system chatbot. "
            "Determine if the user's message is an EXPLICIT REQUEST to schedule or book a demo — "
            "NOT a question about what a demo is, NOT a question about its cost or process. "
            "Examples that ARE a demo request: 'I want a demo', 'can you schedule a demo for me', "
            "'am nevoie de demo', 'vreau sa programez o demonstratie', 'запишите меня на демо'. "
            "Examples that are NOT: 'what is a demo?', 'how much does a demo cost?', "
            "'do you offer demos?', 'cum functioneaza demonstratia?'. "
            "Reply ONLY with: YES | NO. No other text."
        )},
        {"role": "user", "content": text},
    ]
    try:
        result = classifier.chat(messages=messages).strip().upper()
        return result == "YES"
    except Exception:
        return False


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

_LANG_RESPONSE_INSTRUCTION = {
    "ro": "Răspunzi DOAR în română. Nu folosi altă limbă.",
    "en": "Reply ONLY in English. Do not use any other language.",
    "ru": "Отвечай ТОЛЬКО на русском языке. Не используй другие языки.",
}


def _system_prompt(lang: str = "ro") -> str:
    lang_instr = _LANG_RESPONSE_INSTRUCTION.get(lang, _LANG_RESPONSE_INSTRUCTION["ro"])
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
        f"- {lang_instr}\n\n"
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
    system = _system_prompt(lang)
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

def _demo_offer(lang: str = "ro") -> str:
    return {
        "ro": "\n\nDoriți să programați o demonstrație RParking? (da / nu)",
        "en": "\n\nWould you like to schedule an RParking demo? (yes / no)",
        "ru": "\n\nХотите запланировать демонстрацию RParking? (да / нет)",
    }.get(lang, "\n\nDoriți să programați o demonstrație RParking? (da / nu)")


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
# Translated string table
# ---------------------------------------------------------------------------

_S: dict[tuple[str, str], str] = {
    # Demo offer
    ("ro", "demo_offer"): "\n\nDoriți să programați o demonstrație RParking? (da / nu)",
    ("en", "demo_offer"): "\n\nWould you like to schedule an RParking demo? (yes / no)",
    ("ru", "demo_offer"): "\n\nХотите запланировать демонстрацию RParking? (да / нет)",
    # Lead capture — demo flow
    ("ro", "lead_start"): "Super! Cum vă numiți, vă rog?",
    ("en", "lead_start"): "Great! What's your name, please?",
    ("ru", "lead_start"): "Отлично! Как вас зовут?",
    ("ro", "lead_name_invalid"): "Vă rog să îmi spuneți numele dvs. (minim 2 caractere).",
    ("en", "lead_name_invalid"): "Please tell me your name (min. 2 characters).",
    ("ru", "lead_name_invalid"): "Пожалуйста, укажите ваше имя (минимум 2 символа).",
    ("ro", "lead_ask_company"): "Compania / Organizația dvs.?",
    ("en", "lead_ask_company"): "Your company / organization?",
    ("ru", "lead_ask_company"): "Ваша компания / организация?",
    ("ro", "lead_company_invalid"): "Vă rog să îmi spuneți numele companiei (minim 2 caractere).",
    ("en", "lead_company_invalid"): "Please tell me your company name (min. 2 characters).",
    ("ru", "lead_company_invalid"): "Пожалуйста, укажите название компании (минимум 2 символа).",
    ("ro", "lead_ask_phone"): "Numărul dvs. de telefon? (ex: 07xx xxx xxx / +40...)",
    ("en", "lead_ask_phone"): "Your phone number? (e.g. 07xx xxx xxx / +40...)",
    ("ru", "lead_ask_phone"): "Ваш номер телефона? (например: 07xx xxx xxx / +40...)",
    ("ro", "lead_phone_invalid"): "Nu am recunoscut un număr valid. Vă rog să îl scrieți din nou (ex: 07xx xxx xxx / +40...).",
    ("en", "lead_phone_invalid"): "I didn't recognise a valid number. Please enter it again (e.g. 07xx xxx xxx / +40...).",
    ("ru", "lead_phone_invalid"): "Не удалось распознать номер. Пожалуйста, введите снова (например: 07xx xxx xxx / +40...).",
    ("ro", "lead_ask_email"): "Adresa de email? (ex: nume@companie.ro)",
    ("en", "lead_ask_email"): "Your email address? (e.g. name@company.com)",
    ("ru", "lead_ask_email"): "Ваш адрес эл. почты? (например: name@company.com)",
    ("ro", "lead_email_invalid"): "Nu am recunoscut un email valid. Scrieți adresa (ex: nume@companie.ro) sau scrieți 'nu am'.",
    ("en", "lead_email_invalid"): "I didn't recognise a valid email. Enter your address or type 'skip'.",
    ("ru", "lead_email_invalid"): "Не удалось распознать email. Введите адрес или напишите 'нет'.",
    ("ro", "lead_ask_spots"): "Câte locuri de parcare are proiectul dvs.? (număr aproximativ)",
    ("en", "lead_ask_spots"): "How many parking spots does your project have? (approximate number)",
    ("ru", "lead_ask_spots"): "Сколько парковочных мест в вашем проекте? (приблизительно)",
    ("ro", "lead_ask_city"): "În ce oraș/localitate se află parcarea?",
    ("en", "lead_ask_city"): "In what city / location is the parking?",
    ("ru", "lead_ask_city"): "В каком городе / населённом пункте находится парковка?",
    ("ro", "lead_city_invalid"): "Vă rog să îmi spuneți orașul (minim 2 caractere).",
    ("en", "lead_city_invalid"): "Please tell me the city (min. 2 characters).",
    ("ru", "lead_city_invalid"): "Пожалуйста, укажите город (минимум 2 символа).",
    ("ro", "lead_ask_type"): "Ce tip de sistem vă interesează? ({types})",
    ("en", "lead_ask_type"): "What type of system are you interested in? ({types})",
    ("ru", "lead_ask_type"): "Какой тип системы вас интересует? ({types})",
    ("ro", "lead_done"): (
        "Mulțumesc! Am înregistrat datele dvs.\n"
        "Un consultant RParking vă va contacta în cel mai scurt timp pentru a stabili detaliile demonstrației.\n\n"
        "Puteți folosi și calendarul din interfață pentru a alege direct o dată și oră disponibilă.\n\n"
        "Vă mulțumim că ați contactat RParking. O zi bună!"
    ),
    ("en", "lead_done"): (
        "Thank you! We have recorded your details.\n"
        "An RParking consultant will contact you shortly to arrange the demo.\n\n"
        "You can also use the calendar in the interface to choose a date and time directly.\n\n"
        "Thank you for contacting RParking. Have a great day!"
    ),
    ("ru", "lead_done"): (
        "Спасибо! Ваши данные записаны.\n"
        "Консультант RParking свяжется с вами в ближайшее время для согласования деталей демонстрации.\n\n"
        "Вы также можете воспользоваться календарём в интерфейсе для выбора удобных даты и времени.\n\n"
        "Спасибо, что обратились в RParking. Хорошего дня!"
    ),
    ("ro", "lead_error"): "A apărut o problemă la salvarea datelor. Puteți încerca din nou sau ne contactați direct.",
    ("en", "lead_error"): "A problem occurred while saving your data. Please try again or contact us directly.",
    ("ru", "lead_error"): "Произошла ошибка при сохранении данных. Попробуйте снова или свяжитесь с нами напрямую.",
    # Manager transfer
    ("ro", "manager_start"): "Înțeles! Vă voi pune în legătură cu un manager RParking.\nCum vă numiți, vă rog?",
    ("en", "manager_start"): "Understood! I'll connect you with an RParking manager.\nWhat's your name, please?",
    ("ru", "manager_start"): "Понял! Соединю вас с менеджером RParking.\nКак вас зовут?",
    ("ro", "manager_name_invalid"): "Vă rog să îmi spuneți numele dvs. (minim 2 caractere).",
    ("en", "manager_name_invalid"): "Please tell me your name (min. 2 characters).",
    ("ru", "manager_name_invalid"): "Пожалуйста, укажите ваше имя (минимум 2 символа).",
    ("ro", "manager_ask_phone"): "Numărul dvs. de telefon? (ex: 07xx xxx xxx / +40...)",
    ("en", "manager_ask_phone"): "Your phone number? (e.g. 07xx xxx xxx / +40...)",
    ("ru", "manager_ask_phone"): "Ваш номер телефона? (например: 07xx xxx xxx / +40...)",
    ("ro", "manager_phone_invalid"): "Nu am recunoscut un număr valid. Vă rog să îl scrieți din nou (ex: 07xx xxx xxx / +40...).",
    ("en", "manager_phone_invalid"): "I didn't recognise a valid number. Please enter it again (e.g. 07xx xxx xxx / +40...).",
    ("ru", "manager_phone_invalid"): "Не удалось распознать номер. Пожалуйста, введите снова (например: 07xx xxx xxx / +40...).",
    ("ro", "manager_ask_subject"): "Cu ce subiect doriți să vorbiți cu un manager RParking?",
    ("en", "manager_ask_subject"): "What subject would you like to discuss with an RParking manager?",
    ("ru", "manager_ask_subject"): "По какому вопросу вы хотите поговорить с менеджером RParking?",
    ("ro", "manager_subject_invalid"): "Vă rog să descrieți pe scurt subiectul (minim 3 caractere).",
    ("en", "manager_subject_invalid"): "Please briefly describe the subject (min. 3 characters).",
    ("ru", "manager_subject_invalid"): "Пожалуйста, кратко опишите тему (минимум 3 символа).",
    ("ro", "manager_done"): (
        "Mulțumesc! Datele dvs. au fost transmise.\n"
        "Un manager RParking vă va contacta în cel mai scurt timp.\n\n"
        "Vă mulțumim că ați contactat RParking. O zi bună!"
    ),
    ("en", "manager_done"): (
        "Thank you! Your details have been passed on.\n"
        "An RParking manager will contact you shortly.\n\n"
        "Thank you for contacting RParking. Have a great day!"
    ),
    ("ru", "manager_done"): (
        "Спасибо! Ваши данные переданы.\n"
        "Менеджер RParking свяжется с вами в ближайшее время.\n\n"
        "Спасибо, что обратились в RParking. Хорошего дня!"
    ),
    # General
    ("ro", "demo_no"): "Bine, vă pot ajuta cu altceva?",
    ("en", "demo_no"): "Sure, can I help you with anything else?",
    ("ru", "demo_no"): "Хорошо, могу ли я помочь вам с чем-то ещё?",
    ("ro", "no_kb_result"): "Nu am găsit informații relevante pentru întrebarea dvs. în baza noastră de date.",
    ("en", "no_kb_result"): "I couldn't find relevant information for your question in our knowledge base.",
    ("ru", "no_kb_result"): "Не удалось найти информацию по вашему вопросу в нашей базе знаний.",
    ("ro", "conv_ended"): "Această conversație s-a încheiat. Scrieți /new pentru a începe una nouă.",
    ("en", "conv_ended"): "This conversation has ended. Type /new to start a new one.",
    ("ru", "conv_ended"): "Этот разговор завершён. Напишите /new, чтобы начать новый.",
}


def _t(lang: str, key: str) -> str:
    """Return translated string for lang/key, falling back to Romanian."""
    return _S.get((lang, key), _S[("ro", key)])


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


def _handle_manager_transfer(conversation_id: str, user_text: str, meta: dict, lang: str = "ro") -> str | None:
    """Handle 3-step manager transfer collection: name → phone → subject."""
    mt_state = meta.get("manager_transfer") or {}
    if not mt_state.get("active"):
        return None

    step = mt_state.get("step")
    draft = dict(mt_state.get("draft") or {})

    if step == "name":
        name = user_text.strip()
        if len(name) < 2:
            return _t(lang, "manager_name_invalid")
        draft["name"] = name
        mt_state.update({"step": "phone", "draft": draft})
        _store.update_meta(conversation_id, {"manager_transfer": mt_state})
        return _t(lang, "manager_ask_phone")

    if step == "phone":
        if not is_valid_phone(user_text.strip()):
            return _t(lang, "manager_phone_invalid")
        draft["phone"] = user_text.strip()
        mt_state.update({"step": "subject", "draft": draft})
        _store.update_meta(conversation_id, {"manager_transfer": mt_state})
        return _t(lang, "manager_ask_subject")

    if step == "subject":
        subject = user_text.strip()
        if len(subject) < 3:
            return _t(lang, "manager_subject_invalid")
        draft["subject"] = subject
        mt_state.update({"step": None, "active": False, "draft": draft})
        _store.update_meta(conversation_id, {"manager_transfer": mt_state, "stage": "ended"})

        try:
            send_manager_transfer_notification(
                name=draft.get("name", ""),
                phone=draft.get("phone", ""),
                subject=subject,
                lang=lang,
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

        return _t(lang, "manager_done")

    return None


# ---------------------------------------------------------------------------
# Lead capture flow handler
# ---------------------------------------------------------------------------

def _handle_lead_capture(conversation_id: str, user_text: str, meta: dict, lang: str = "ro") -> str | None:
    """Handle multi-step lead contact collection for RParking demo requests."""
    lead_state = meta.get("lead") or {}
    if not lead_state.get("active"):
        return None

    step = lead_state.get("step")
    draft = dict(lead_state.get("draft") or {})

    if step == "name":
        name = user_text.strip()
        if len(name) < 2:
            return _t(lang, "lead_name_invalid")
        draft["name"] = name
        lead_state.update({"step": "company", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return _t(lang, "lead_ask_company")

    if step == "company":
        company = user_text.strip()
        if len(company) < 2:
            return _t(lang, "lead_company_invalid")
        draft["company"] = company
        lead_state.update({"step": "phone", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return _t(lang, "lead_ask_phone")

    if step == "phone":
        if not is_valid_phone(user_text.strip()):
            return _t(lang, "lead_phone_invalid")
        draft["phone"] = user_text.strip()
        lead_state.update({"step": "email", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return _t(lang, "lead_ask_email")

    if step == "email":
        t = _normalize(user_text)
        if any(k in t for k in ["nu am", "skip", "fara", "fără", "sari", "none", "no email", "нет", "пропустить", "без"]):
            draft["email"] = None
        else:
            email = _extract_email(user_text)
            if not email or not is_valid_email(email):
                return _t(lang, "lead_email_invalid")
            draft["email"] = email
        lead_state.update({"step": "nr_spots", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return _t(lang, "lead_ask_spots")

    if step == "nr_spots":
        m = re.search(r"\d+", user_text)
        draft["nr_parking_spots"] = int(m.group(0)) if m else None
        lead_state.update({"step": "city", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return _t(lang, "lead_ask_city")

    if step == "city":
        city = user_text.strip()
        if len(city) < 2:
            return _t(lang, "lead_city_invalid")
        draft["city"] = city
        display_types = _PROJECT_TYPES_DISPLAY.get(lang, _PROJECT_TYPES_DISPLAY["ro"])
        types_str = " / ".join(display_types)
        lead_state.update({"step": "project_type", "draft": draft})
        _store.update_meta(conversation_id, {"lead": lead_state})
        return _t(lang, "lead_ask_type").format(types=types_str)

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
            return _t(lang, "lead_error")

        try:
            send_lead_notification(lead, lang)
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

        return _t(lang, "lead_done")

    return None


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@bp.post("/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    conversation_id = payload.get("conversation_id")
    message = payload.get("message")

    # First call: create conversation and return greeting (always Romanian)
    if not conversation_id:
        greeting = _greeting()
        conversation_id = _store.create(initial_messages=[
            {"role": "system", "content": _system_prompt("ro")},
            {"role": "assistant", "content": greeting},
        ])
        _store.update_meta(conversation_id, {
            "stage": "chatting",
            "lang": "ro",
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
        lang_ended = meta.get("lang", "ro")
        return jsonify({
            "conversation_id": conversation_id,
            "reply": _t(lang_ended, "conv_ended"),
        })

    api_key = current_app.config.get("OPENAI_API_KEY", "")
    model = current_app.config.get("OPENAI_MODEL", "gpt-4o-mini")

    # ── Language detection (locked during any active capture flow) ────────────
    is_capture_active = (
        meta.get("lead", {}).get("active")
        or meta.get("manager_transfer", {}).get("active")
    )
    if is_capture_active:
        lang = meta.get("lang", "ro")
    else:
        detected = _detect_language(user_text)
        _msg_words = user_text.strip().split()
        # Only switch language on substantive messages; short replies (yes/no/ok)
        # inherit the stored language to avoid false-switching on single words.
        if (len(_msg_words) >= 3
                or detected == "ru"
                or any(c in user_text for c in _RO_DIACRITICS)):
            lang = detected
            _store.update_meta(conversation_id, {"lang": lang})
        else:
            lang = meta.get("lang", "ro")

    def _respond(reply: str) -> object:
        _store.append(conversation_id, {"role": "assistant", "content": reply})
        return jsonify({"conversation_id": conversation_id, "reply": reply})

    # ── Active lead capture flow (highest priority) ───────────────────────────
    lead_reply = _handle_lead_capture(conversation_id, user_text, meta, lang)
    if lead_reply is not None:
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(lead_reply)

    # ── Active manager transfer flow ──────────────────────────────────────────
    mt_reply = _handle_manager_transfer(conversation_id, user_text, meta, lang)
    if mt_reply is not None:
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(mt_reply)

    # ── Manager intent check (only when no capture is active) ─────────────────
    if _detect_manager_intent(user_text, api_key, model):
        _store.update_meta(conversation_id, {
            "manager_transfer": {"active": True, "step": "name", "draft": {}},
        })
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(_t(lang, "manager_start"))

    # ── Demo request intent check ─────────────────────────────────────────────
    if _detect_demo_request_intent(user_text, api_key, model):
        _store.update_meta(conversation_id, {
            "lead": {"active": True, "step": "name", "draft": {}},
            "stage": "lead_capture",
        })
        _store.append(conversation_id, {"role": "user", "content": user_text})
        return _respond(_t(lang, "lead_start"))

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
            return _respond(_t(lang, "lead_start"))
        if yn == "NO":
            _store.append(conversation_id, {"role": "user", "content": user_text})
            return _respond(_t(lang, "demo_no"))
        # UNKNOWN → user ignored offer, fall through to answer normally

    # KB + LLM answer
    try:
        snippets = _kb_search(user_text)
    except ValueError as exc:
        return jsonify({"error": "config_error", "message": str(exc)}), 500

    _store.append(conversation_id, {"role": "user", "content": user_text})
    history = _store.get(conversation_id)

    if not snippets:
        reply = _t(lang, "no_kb_result")
    else:
        try:
            reply = _llm_reply(
                user_text=user_text, history=history, kb_snippets=snippets,
                api_key=api_key, model=model, lang=lang,
            )
        except ValueError as exc:
            return jsonify({"error": "config_error", "message": str(exc)}), 500

    offer = _demo_offer(lang)
    offer_marker = offer.strip()
    if offer_marker in reply:
        reply = reply[:reply.index(offer_marker)].rstrip()

    if snippets:
        return _respond(reply.rstrip() + offer)
    return _respond(reply.rstrip())
