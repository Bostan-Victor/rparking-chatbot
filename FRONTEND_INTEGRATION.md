# RParking Chatbot — Frontend Integration Guide

This document describes every API endpoint the frontend needs to integrate the chatbot widget and the demo-reservation calendar into the RParking website.

---

## Base URL

```
http://<server-host>:5000/api
```

Replace `<server-host>` with the production domain or IP. In local development this is `http://localhost:5000/api`.

CORS is enabled on the server — all origins are accepted.

---

## 1. Chatbot

### Overview of the conversation lifecycle

```
1. Frontend loads  →  POST /api/chat  { "lang": "en" }
                   ←  { conversation_id, reply }   ← bot greeting in English

2. User sends msg  →  POST /api/chat  { conversation_id, message }
                   ←  { conversation_id, reply }

3. Repeat step 2 until the conversation ends.
```

The bot handles everything internally:
- Language is set on init via `lang`; subsequent messages auto-detect language per-message.
- Lead capture flow (name → phone → city → project type).
- Manager transfer flow (name → phone → subject).
- After either flow completes the conversation is **ended** and further messages receive a "conversation ended" reply.

---

### `POST /api/chat`

#### Start a new conversation (no `conversation_id`)

Pass an optional `lang` to set the bot's language for the entire conversation.

| Field | Type | Required | Values |
|---|---|---|---|
| `lang` | string | no | `"ro"` (default), `"en"`, `"ru"` |

```json
{ "lang": "en" }
```

If `lang` is omitted or invalid, it defaults to `"ro"`. The bot's greeting and system prompt are immediately set to the requested language. Subsequent messages still auto-detect language per-message, so the language can shift naturally during the conversation.

**Response `200`:**

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "reply": "Hello! I'm the RParking virtual assistant.\nI can help you with information about our parking management solutions.\n\nHow can I help you today?"
}
```

Store `conversation_id` — it must be sent with every subsequent request.

---

#### Send a message

**Request body:**

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "What systems do you offer?"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `conversation_id` | string | yes | UUID returned from the first call |
| `message` | string | yes | The user's text |

**Response `200`:**

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "reply": "RParking offers four main system types: ..."
}
```

Always render `reply` as the bot's message in the chat UI.

---

#### Error responses

| HTTP | `error` field | When |
|---|---|---|
| `400` | `invalid_request` | `message` is missing or empty |
| `400` | `unknown_conversation` | `conversation_id` does not exist |
| `500` | `config_error` | Server misconfiguration (API key missing etc.) |

```json
{
  "error": "unknown_conversation",
  "message": "Unknown conversation_id. Start a new conversation without conversation_id."
}
```

---

### Conversation stages (internal — for reference only)

The frontend does **not** need to track stages. The bot drives the flow entirely through `reply` text. The stages are:

| Stage | What happens |
|---|---|
| `chatting` | Normal Q&A; bot may append a demo offer at the end of replies |
| `lead_capture` | Bot collects name → phone → city → project type |
| `manager_transfer` | Bot collects name → phone → subject |
| `ended` | Conversation closed; all further messages receive a "conversation ended" reply |

---

### Demo offer inside replies

When the bot decides to offer a demo, it appends a question to its reply:

```
... (answer text) ...

Doriți să programați o demonstrație RParking? (da / nu)
```

The user's "yes" / "no" response is sent as a normal message. The bot interprets it and either starts the lead capture flow or acknowledges the refusal. **No special handling required from the frontend.**

---

### Calendar widget trigger

When lead capture completes successfully, `reply` contains a message that includes the phrase:

- Romanian: `"calendarul din interfață"`
- English: `"calendar in the interface"`
- Russian: `"календарём в интерфейсе"`

**On receiving this reply, open/show the demo-reservation calendar widget** so the user can book a specific slot. The calendar widget uses the two endpoints described in Section 2.

```js
const CALENDAR_TRIGGER_STRINGS = [
  "calendarul din interfață",
  "calendar in the interface",
  "календарём в интерфейсе",
];

function shouldShowCalendar(reply) {
  return CALENDAR_TRIGGER_STRINGS.some(s => reply.includes(s));
}
```

---

## 2. Demo Reservation Calendar

### `GET /api/demo-reservation/slots`

Returns available time slots for a given date from the Google Calendar.

**Query parameter:**

| Param | Type | Example |
|---|---|---|
| `date` | string (YYYY-MM-DD) | `2025-09-15` |

**Request:**

```
GET /api/demo-reservation/slots?date=2025-09-15
```

**Response `200`:**

```json
{
  "date": "2025-09-15",
  "slots": [
    "2025-09-15T09:00:00",
    "2025-09-15T10:00:00",
    "2025-09-15T14:00:00"
  ]
}
```

`slots` is an array of ISO 8601 datetime strings representing free slots. May be an empty array if no slots are available that day.

**Error responses:**

| HTTP | `error` | When |
|---|---|---|
| `400` | `invalid_request` | `date` missing or wrong format |
| `500` | `config_error` | Calendar not configured on server |
| `500` | `calendar_error` | Google Calendar API failure |

---

### `POST /api/demo-reservation`

Books a demo slot. Creates a Google Calendar event, saves to DB, sends Telegram notification and a confirmation email to the user.

**Request body:**

```json
{
  "name": "Ion Popescu",
  "phone": "068123456",
  "email": "ion@exemplu.md",
  "project_type": "Sistem complet",
  "datetime": "2025-09-15T10:00:00"
}
```

| Field | Type | Required | Constraints |
|---|---|---|---|
| `name` | string | yes | min 2 characters |
| `phone` | string | yes | Moldova format: `0XXXXXXXX` (9 digits) or `+373XXXXXXXX` (11 digits), exactly 8 significant digits |
| `email` | string | yes | valid email address |
| `project_type` | string | yes | one of the values listed below |
| `datetime` | string | yes | ISO 8601, must be in the future |

**Allowed `project_type` values:**

```
"Sistem complet"
"Sistem CardPass"
"Sistem cu tichete"
"Sistem QR Code"
"Altul"
```

**Response `200`:**

```json
{
  "status": "ok",
  "message": "Rezervarea a fost confirmată! Veți primi un email de confirmare.",
  "reservation_id": 42
}
```

**Error responses:**

| HTTP | `error` | When |
|---|---|---|
| `400` | `invalid_request` | Any field missing, invalid, or `datetime` in the past |
| `500` | `config_error` | Calendar not configured |
| `500` | `calendar_error` | Google Calendar API failure |
| `500` | `db_error` | Database error |

---

## 3. Health Check

```
GET /api/health
```

**Response `200`:**

```json
{ "status": "ok" }
```

Use this to check if the backend is reachable before initialising the chat widget.

---

## 4. Minimal Integration Example (JavaScript)

```js
const API_BASE = "http://<server-host>:5000/api";

let conversationId = null;

// Call once on widget open — pass the site's current language
async function startChat(lang = "ro") {   // "ro" | "en" | "ru"
  const res = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lang }),
  });
  const data = await res.json();
  conversationId = data.conversation_id;
  displayBotMessage(data.reply);
}

// Call on every user message
async function sendMessage(userText) {
  const res = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      conversation_id: conversationId,
      message: userText,
    }),
  });
  const data = await res.json();

  if (data.error) {
    console.error(data.error, data.message);
    return;
  }

  displayBotMessage(data.reply);

  // Show calendar widget when lead capture ends
  if (shouldShowCalendar(data.reply)) {
    openCalendarWidget();
  }
}

// Load available slots for a date
async function loadSlots(date) {           // date: "YYYY-MM-DD"
  const res = await fetch(`${API_BASE}/demo-reservation/slots?date=${date}`);
  const data = await res.json();
  return data.slots;                       // string[] of ISO datetimes
}

// Submit a booking
async function bookSlot(name, phone, email, projectType, datetime) {
  const res = await fetch(`${API_BASE}/demo-reservation`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, phone, email, project_type: projectType, datetime }),
  });
  const data = await res.json();
  if (data.status === "ok") {
    showConfirmation(data.message);
  } else {
    showError(data.message);
  }
}
```

---

## 5. Key Rules & Edge Cases

- **Always store `conversation_id`** from the first response and send it with every subsequent request.
- **Pass `lang` on init** matching the website's current language setting (`"ro"`, `"en"`, or `"ru"`). The bot greets the user and initialises its system prompt in that language immediately.
- **Do not re-initiate** a conversation with the same `conversation_id` after the conversation has ended — the bot will reply with a "conversation ended" message. Start fresh with a new `POST /api/chat { "lang": "..." }`.
- **The bot drives all form flows** (lead capture, manager transfer) through plain text replies. The frontend only needs to render `reply` and send user text — no special field rendering is needed inside the chat.
- **Calendar widget is optional** — the user receives a confirmation reply from the bot regardless. The calendar is an additional convenience for picking a specific time slot.
- **Language auto-detection** — after the initial greeting, the bot detects the language of each message and can switch naturally if the user writes in a different language.
