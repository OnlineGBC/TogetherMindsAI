# Session ID Reference — TogetherMindsAI
*Captured 2026-04-12. Use this as the authoritative source before touching anything session-related.*

---

## 1. The Three Session Modes

| Mode   | Who creates the session | Session ID type | URL pattern |
|--------|------------------------|-----------------|-------------|
| Solo   | Created per-user at registration | Full UUID (same as user_id) | `/therapy/solo` (no ID in URL) |
| Couple | Created per-user at registration; partner joins by ID | Full UUID (same as creator's user_id) | `/therapy/couple/<session_id>` |
| Group  | Created per-user at registration | randomized private key (e.g. `AB3K7M`) | `/therapy/group/<session_id>` |

---

## 2. The Two ID Concepts

### `user_id`
- A full UUID (e.g. `7a6d1ebd-e6b6-4b7d-afbf-9c56984b34f7`)
- One per person, generated at registration
- Stored in the Flask session cookie (`session["user_id"]`)
- Used as the DB primary key for `users`, `exercises`, `chat_messages`
- **Never shown in the browser URL** (masked since the URL-masking change)

### `session_id`
- Identifies the shared conversation room
- **Solo**: `session_id == user_id` (the user's UUID)
- **Couple**: `session_id == creator's user_id` (a UUID)
- **Group**: a randomized private key string from charset `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no 0/O, 1/I/l)
- Appears in the URL for couple and group
- Used as PK in `therapy_sessions` table

### `display_id` (template variable)
- A short human-readable code shown in the UI
- **Solo**: `_short_id(user_id)` → first 6 chars of UUID with hyphens stripped and uppercased (e.g. `7A6D1E`)
- **Couple**: `_short_id(session_id)` → same transformation on the couple UUID
- **Group**: `session_id` directly (already a randomized private key)
- Passed to every therapy template as `display_id=...`
- Shown in the session ID banner and privacy banner
- Used as the localStorage key for dismissing the privacy banner and storing nickname

```python
def _short_id(id_str: str) -> str:
    return id_str.replace("-", "").upper()[:6]
```

---

## 3. Registration & Auth Flow (Browser JS path — used by most users)

```
/auth/<mode>  →  user checks consent boxes, clicks Continue
    │
    └─ auth.js: startAuth(mode, onSuccess, onError)
           │
           ├─ New user: POST /api/auth/register  { public_key, therapy_mode }
           │       → server creates User + TherapySession records, sets session["user_id"]
           │       → returns { user_id, therapy_mode, session_id? }
           │
           └─ Returning user: POST /api/auth/challenge → POST /api/auth/verify
                   → server sets session["user_id"]
                   → returns { ok, user_id, therapy_mode, session_id? }
           │
           └─ onSuccess(data):
                   solo   → window.location.href = "/therapy/solo"
                   couple → window.location.href = "/therapy/couple/" + data.session_id
                   group  → window.location.href = "/therapy/group/" + data.session_id
```

### What the API register response includes

| Mode   | `session_id` in response |
|--------|--------------------------|
| Solo   | *(absent / null)*        |
| Couple (new session) | `user_id` (the creator's UUID becomes the couple session ID) |
| Couple (joining existing) | the existing couple `session_id` from `pending_couple_session` cookie |
| Group (new session) | the randomized private key |
| Group (joining existing) | the existing group `session_id` from `pending_group_session` cookie |

### What the API verify response includes

| Mode   | `session_id` in response |
|--------|--------------------------|
| Solo   | `null`                   |
| Couple | `user_id` (always — couple session_id == user_id) |
| Group  | `ts.id` if a TherapySession exists for this user, else `null` |

---

## 4. Legacy Form-Based Auth Flow (fallback, used when WebCrypto unavailable)

```
POST /auth/<mode>  →  server creates User + TherapySession + sets session["user_id"]
    │
    ├─ solo   → redirect /therapy/solo
    ├─ couple → redirect /therapy/couple/<session_id>   (session_id = user_id if new)
    └─ group  → redirect /therapy/group/<session_id>    (session_id = randomized private key)
```

---

## 5. Joining an Existing Session

### Flow

```
GET  /session/join          → renders join form
POST /session/join  { session_id: "AB3K7M" or "My nickname" }
```

Server looks up `TherapySession` by:
1. Exact match on `session_id` (case-sensitive)
2. Case-insensitive match on `therapy_sessions.nickname` column

If not found → error "Session not found."

If found:

| Mode   | Has `session["user_id"]`? | Action |
|--------|--------------------------|--------|
| Solo   | either | `session["user_id"] = session_id` then redirect `/therapy/solo` |
| Couple | yes | redirect `/therapy/couple/<session_id>` |
| Couple | no  | stash `session["pending_couple_session"] = session_id`, redirect `/auth/couple` |
| Group  | yes | redirect `/therapy/group/<session_id>` |
| Group  | no  | stash `session["pending_group_session"] = session_id`, redirect `/auth/group` |

The pending session cookie is read by both `auth_post` (legacy) and `api_auth_register` (JS) and cleared once used.

---

## 6. Route → Template Variable Mapping

### `/therapy/solo` (GET)
```python
user_id    = session["user_id"]         # full UUID
display_id = _short_id(user_id)         # 6-char code
render_template("solo.html",
    messages=..., user_id=user_id, display_id=display_id)
```

### `/therapy/couple/<session_id>` (GET)
```python
user_id    = session["user_id"]         # full UUID of current user
session_id = <from URL>                 # full UUID (creator's user_id)
display_id = _short_id(session_id)      # 6-char code
render_template("couple.html",
    user_id=user_id, session_id=session_id, display_id=display_id)
```

### `/therapy/group/<session_id>` (GET)
```python
user_id    = session["user_id"]         # full UUID of current user
session_id = <from URL>                 # randomized private key (already short)
display_id = session_id                 # same — no transformation needed
render_template("group.html",
    user_id=user_id, session_id=session_id, display_id=display_id)
```

---

## 7. Template JS Variables

Each therapy template defines these JS vars at the top of `{% block scripts %}`:

| Template    | JS variable        | Value                     |
|-------------|-------------------|---------------------------|
| solo.html   | `var _DISPLAY_ID` | `"{{ display_id }}"`      |
| solo.html   | *(no SESSION_ID)* | *(not needed)*            |
| couple.html | `var USER_ID`     | `"{{ user_id }}"`         |
| couple.html | `var SESSION_ID`  | `"{{ session_id }}"`      |
| couple.html | `var DISPLAY_ID`  | `"{{ display_id }}"`      |
| group.html  | `var USER_ID`     | `"{{ user_id }}"`         |
| group.html  | `var SESSION_ID`  | `"{{ session_id }}"`      |
| group.html  | `var DISPLAY_ID`  | `"{{ display_id }}"`      |

---

## 8. Nickname System

- User types a name via the pencil (✏️) button → saved to `localStorage` key `session_nickname_<SESSION_ID>`
- Also saved server-side: `POST /session/<session_id>/nickname { nickname }` → stored in `therapy_sessions.nickname`
- On `/session/join`, the server does a case-insensitive lookup on `therapy_sessions.nickname` so users can rejoin by friendly name instead of code
- The display in the session banner renders as: **CODE · "Friendly Name"**

---

## 9. SocketIO Room Identity

For couple and group, the real-time chat uses SocketIO rooms keyed on `session_id`:
- Join event: `{ session_id, user_id, mode }`
- Message event: `{ session_id, user_id, text }`
- The room name is the `session_id` string

Solo does NOT use SocketIO — it uses plain HTTP POST to `/therapy/solo`.

---

## 10. Known Issues & Change History (this conversation)

### What was changed (all in the same branch, currently on `main`):
1. **Solo URL**: changed from `/therapy/solo/<user_id>` to `/therapy/solo` (user_id read from Flask cookie)
2. **Couple URL**: changed from `/therapy/couple/<user_id>?session_id=...` to `/therapy/couple/<session_id>`
3. **Group URL**: changed from `/therapy/group/<user_id>/<session_id>` to `/therapy/group/<session_id>`
4. **Group session ID format**: changed from 4-digit int to randomized private key
5. **display_id**: introduced `_short_id()` helper and `display_id` template variable
6. **auth.html JS redirects**: updated to match new URL patterns
7. **Session banner**: `sessionNicknameDisplay` moved inline with code span (so both show together)
8. **Privacy banner**: `display_id` span given `id="privacyBannerCode"`; JS fallback populates it from `_DISPLAY_ID`/`DISPLAY_ID`

### Remaining concern flagged by user:
- Previously-fixed behaviours regressing — suspected cause is complexity of interacting changes (URL masking, display_id, nickname JS, session cookie vs URL-based identity) compounding across sessions

---

## 11. Database Tables Involved

| Table              | Key columns relevant to sessions |
|--------------------|----------------------------------|
| `users`            | `id` (UUID), `therapy_mode`      |
| `therapy_sessions` | `id` (session_id), `mode`, `created_by`, `nickname` |
| `chat_messages`    | `session_id`, `user_id`, `text`, `timestamp` |
| `exercises`        | `user_id`, `mode`, `prompt`, `response` |

---

## 12. Files to Read When Debugging

| File | What it controls |
|------|-----------------|
| `TogetherMindsAI.py` | All routes, session logic, `_short_id()` |
| `static/js/auth.js` | Browser-side auth flow, `startAuth()`, redirects after auth |
| `static/js/therapy.js` | SocketIO, `initSessionNickname()`, `initEndSessionGuard()` |
| `templates/auth.html` | Auth page + JS redirect after auth (lines ~136–147) |
| `templates/solo.html` | Solo therapy UI; form POSTs to `/therapy/solo` |
| `templates/couple.html` | Couple therapy UI; SocketIO-based |
| `templates/group.html` | Group therapy UI; SocketIO-based |
| `templates/join_session.html` | /session/join form |
| `models.py` | `User`, `TherapySession`, `ChatMessage`, `Exercise` |
