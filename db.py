"""
Supabase helpers for LearnMate Classroom: accounts, quiz results and teacher notes.

Uses the Supabase REST API through `requests`, so no extra package is needed.
All calls happen on the server with the SERVICE key. That key must only live in
Streamlit Secrets (never in GitHub and never in the browser).

Secrets used (Streamlit "Secrets" or environment variables):
    SUPABASE_URL           e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY   the service_role key (secret!)
    TEACHER_INVITE_CODE    code that teachers type when creating a teacher account
"""

import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import requests

NOTES_BUCKET = "notes"
MAX_FAILED = 5          # wrong passwords before the account is locked for a while
LOCK_MINUTES = 10
TIMEOUT = 15

NICK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{2,19}$")
GENERIC_LOGIN_ERROR = "Wrong class, nickname or password. Please try again."


class DBError(Exception):
    """Raised when the database or storage cannot be reached or refuses a request."""


# ----------------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------------
def _url():
    return os.getenv("SUPABASE_URL", "").strip().rstrip("/")


def _key():
    return os.getenv("SUPABASE_SERVICE_KEY", "").strip()


def enabled():
    return bool(_url() and _key())


def _headers(extra=None):
    headers = {"apikey": _key(), "Authorization": f"Bearer {_key()}"}
    if extra:
        headers.update(extra)
    return headers


def _rest(method, table, params=None, json=None, prefer=None):
    headers = _headers({"Content-Type": "application/json"})
    if prefer:
        headers["Prefer"] = prefer
    try:
        resp = requests.request(
            method, f"{_url()}/rest/v1/{table}",
            params=params, json=json, headers=headers, timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise DBError("Could not reach the database.") from e
    if resp.status_code >= 300:
        print(f"Database error {resp.status_code} on {method} {table}: {resp.text[:300]}")
        try:
            detail = str(resp.json().get("message") or "")[:160]
        except Exception:
            detail = ""
        raise DBError(f"{resp.status_code} on {table}: {detail}".strip())
    return resp.json() if resp.content else []


# ----------------------------------------------------------------------------
# Passwords (stored hashed with scrypt, never as plain text)
# ----------------------------------------------------------------------------
def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


_DUMMY_HASH = hash_password("not-a-real-password")


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------
def clean_nickname(nickname):
    return re.sub(r"\s+", " ", (nickname or "").strip())


def validate_password(password):
    if len(password or "") < 6:
        return "Your password must have at least 6 characters."
    if len(password) > 72:
        return "That password is too long (maximum 72 characters)."
    return ""


def validate_signup(nickname, password, confirm):
    nick = clean_nickname(nickname)
    if not NICK_RE.match(nick):
        return ("Your nickname must be 3 to 20 characters: letters, numbers, spaces, - or _. "
                "Please do not use your full real name.")
    err = validate_password(password)
    if err:
        return err
    if password != confirm:
        return "The two passwords are not the same."
    return ""


# ----------------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------------
def _public_user(row):
    return {
        "id": row["id"],
        "class_level": row["class_level"],
        "nickname": row["nickname"],
        "role": row["role"],
    }


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def create_account(class_level, nickname, password, role, consent):
    """Returns (user_dict_or_None, error_message)."""
    nick = clean_nickname(nickname)
    key = nick.lower()
    existing = _rest("GET", "profiles", params={
        "class_level": f"eq.{class_level}", "nickname_key": f"eq.{key}", "select": "id",
    })
    if existing:
        return None, "That nickname is already used in this class. Please choose another one."
    created = _rest("POST", "profiles", json={
        "class_level": class_level,
        "nickname": nick,
        "nickname_key": key,
        "password_hash": hash_password(password),
        "role": role,
        "consent": bool(consent),
    }, prefer="return=representation")
    return _public_user(created[0]), ""


def authenticate(class_level, nickname, password):
    """Returns (user_dict_or_None, error_message)."""
    key = clean_nickname(nickname).lower()
    rows = _rest("GET", "profiles", params={
        "class_level": f"eq.{class_level}", "nickname_key": f"eq.{key}", "select": "*",
    })
    if not rows:
        verify_password(password, _DUMMY_HASH)  # same effort whether or not the nickname exists
        return None, GENERIC_LOGIN_ERROR
    user = rows[0]

    locked_until = _parse_time(user.get("locked_until"))
    now = datetime.now(timezone.utc)
    if locked_until and locked_until > now:
        minutes = int((locked_until - now).total_seconds() // 60) + 1
        return None, f"Too many wrong tries. Please wait about {minutes} minute(s), or ask your teacher to reset your password."

    if verify_password(password, user["password_hash"]):
        if user.get("failed_attempts"):
            _rest("PATCH", "profiles", params={"id": f"eq.{user['id']}"},
                  json={"failed_attempts": 0, "locked_until": None})
        return _public_user(user), ""

    fails = (user.get("failed_attempts") or 0) + 1
    if fails >= MAX_FAILED:
        until = (now + timedelta(minutes=LOCK_MINUTES)).isoformat()
        _rest("PATCH", "profiles", params={"id": f"eq.{user['id']}"},
              json={"failed_attempts": 0, "locked_until": until})
    else:
        _rest("PATCH", "profiles", params={"id": f"eq.{user['id']}"},
              json={"failed_attempts": fails})
    return None, GENERIC_LOGIN_ERROR


def reset_password(user_id, new_password):
    _rest("PATCH", "profiles", params={"id": f"eq.{user_id}"}, json={
        "password_hash": hash_password(new_password),
        "failed_attempts": 0,
        "locked_until": None,
    })


def list_learners():
    return _rest("GET", "profiles", params={
        "role": "eq.learner", "select": "id,nickname,class_level",
        "order": "class_level.asc,nickname.asc", "limit": "2000",
    })


# ----------------------------------------------------------------------------
# Quiz results
# ----------------------------------------------------------------------------
def save_result(user, subject, topic, score, total, details=None, class_level=None):
    """details = list of {question, options, correct, picked, explanation} for each question.
    Quizzes taken by teachers are saved too, but marked as tests so they can be hidden."""
    _rest("POST", "quiz_results", json={
        "user_id": user["id"],
        "nickname": user["nickname"],
        "class_level": class_level or user["class_level"],
        "subject": subject,
        "topic": topic,
        "score": int(score),
        "total": int(total),
        "details": details or [],
        "is_test": user.get("role") == "teacher",
    })


def get_results(user_id):
    return _rest("GET", "quiz_results", params={
        "user_id": f"eq.{user_id}",
        "select": "class_level,subject,topic,score,total,created_at",
        "order": "created_at.asc", "limit": "500",
    })


def all_results(limit=2000):
    return _rest("GET", "quiz_results", params={
        "select": "nickname,class_level,subject,topic,score,total,created_at,details,is_test",
        "order": "created_at.desc", "limit": str(limit),
    })


def _safe_cell(value):
    """Stops spreadsheet programs from treating text as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def attempt_rows(results):
    """One row per question answered, with the learner's answer, the correct answer and the explanation."""
    letters = "ABCD"
    out = []
    for r in results:
        base = {
            "Date": str(r.get("created_at", ""))[:16].replace("T", " "),
            "Class": r.get("class_level", ""),
            "Learner": r.get("nickname", ""),
            "Subject": r.get("subject", ""),
            "Topic": _safe_cell(r.get("topic", "")),
            "Quiz score": f"{r.get('score', 0)}/{r.get('total', 0)}",
        }
        details = r.get("details") or []
        if not details:
            out.append({**base, "Question no.": "", "Question": "(question details were not saved for this quiz)",
                        "Learner's answer": "", "Correct answer": "", "Result": "", "Explanation": ""})
            continue
        for i, d in enumerate(details, 1):
            opts = d.get("options") or []

            def opt(ix):
                if isinstance(ix, int) and 0 <= ix < len(opts):
                    return _safe_cell(f"{letters[ix]}. {opts[ix]}")
                return ""

            picked, correct = d.get("picked"), d.get("correct")
            out.append({
                **base,
                "Question no.": i,
                "Question": _safe_cell(d.get("question", "")),
                "Learner's answer": opt(picked),
                "Correct answer": opt(correct),
                "Result": "Correct" if picked == correct else "Wrong",
                "Explanation": _safe_cell(d.get("explanation", "")),
            })
    return out


# ----------------------------------------------------------------------------
# Assignments
# ----------------------------------------------------------------------------
def create_assignments(title, instructions, subject, classes, due_date, teacher_id):
    """One row per class, so each class's assignment can be tracked separately."""
    rows = [{
        "title": title, "instructions": instructions, "subject": subject,
        "class_level": c, "due_date": due_date or None, "created_by": teacher_id,
    } for c in classes]
    _rest("POST", "assignments", json=rows)


def list_assignments(class_level=None):
    params = {"select": "*", "order": "created_at.desc", "limit": "500"}
    if class_level:
        params["class_level"] = f"eq.{class_level}"
    return _rest("GET", "assignments", params=params)


def delete_assignment(assignment_id):
    _rest("DELETE", "assignments", params={"id": f"eq.{assignment_id}"})


def my_submissions(user_id):
    return _rest("GET", "submissions", params={
        "user_id": f"eq.{user_id}", "select": "assignment_id,answer,submitted_at", "limit": "1000",
    })


def submit_assignment(assignment_id, user, answer):
    _rest("POST", "submissions", params={"on_conflict": "assignment_id,user_id"}, json={
        "assignment_id": assignment_id,
        "user_id": user["id"],
        "nickname": user["nickname"],
        "class_level": user["class_level"],
        "answer": answer,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }, prefer="resolution=merge-duplicates")


def list_submissions():
    return _rest("GET", "submissions", params={
        "select": "assignment_id,nickname,class_level,answer,submitted_at",
        "order": "submitted_at.asc", "limit": "5000",
    })


# ----------------------------------------------------------------------------
# Teacher notes (files in Supabase Storage + a row in the notes table)
# ----------------------------------------------------------------------------
def safe_filename(name):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._") or "note"
    return base[-80:]


def _storage(method, path, **kwargs):
    try:
        resp = requests.request(
            method, f"{_url()}/storage/v1/{path}", timeout=60, **kwargs
        )
    except requests.RequestException as e:
        raise DBError("Could not reach file storage.") from e
    if resp.status_code >= 300:
        print(f"Storage error {resp.status_code} on {method} {path}: {resp.text[:300]}")
        raise DBError(f"Storage error {resp.status_code}")
    return resp


def upload_note(data, filename, title, class_level, subject, uploader_id):
    path = f"{class_level}/{uuid.uuid4().hex[:12]}_{safe_filename(filename)}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    _storage(
        "POST", f"object/{NOTES_BUCKET}/{path}", data=data,
        headers=_headers({"Content-Type": content_type, "x-upsert": "false"}),
    )
    try:
        _rest("POST", "notes", json={
            "title": title,
            "class_level": class_level,
            "subject": subject,
            "filename": filename,
            "storage_path": path,
            "uploaded_by": uploader_id,
        })
    except DBError:
        try:
            _storage("DELETE", f"object/{NOTES_BUCKET}/{path}", headers=_headers())
        except DBError:
            pass
        raise


def list_notes():
    return _rest("GET", "notes", params={
        "select": "id,title,class_level,subject,filename,storage_path,created_at",
        "order": "class_level.asc,title.asc", "limit": "1000",
    })


def download_note(storage_path):
    resp = _storage(
        "GET", f"object/authenticated/{NOTES_BUCKET}/{storage_path}", headers=_headers()
    )
    return resp.content


def delete_note(note_id, storage_path):
    _storage("DELETE", f"object/{NOTES_BUCKET}/{storage_path}", headers=_headers())
    _rest("DELETE", "notes", params={"id": f"eq.{note_id}"})


# ----------------------------------------------------------------------------
# Class room: chat messages and live video sessions
# ----------------------------------------------------------------------------
MAX_MESSAGE_CHARS = 500
VIDEO_BASE_URL = "https://meet.jit.si"
SESSION_MAX_HOURS = 6

_PHONE_RE = re.compile(r"(?:\+?\d[\s\-().]*){9,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LINK_RE = re.compile(r"(https?://|www\.)\S+", re.I)


def contains_personal_info(text):
    """True when a learner's message seems to hold a phone number, email or link."""
    return bool(_PHONE_RE.search(text) or _EMAIL_RE.search(text) or _LINK_RE.search(text))


def clean_message(text):
    return re.sub(r"\s+", " ", (text or "")).strip()[:MAX_MESSAGE_CHARS]


def post_message(user, body, thread_id=None, class_level=None):
    """Learners post only into their own thread. Teachers may broadcast
    (thread_id None) or reply into a learner's thread."""
    body = clean_message(body)
    if not body:
        raise DBError("Write a message first.")
    role = user.get("role")
    if role != "teacher":
        thread_id = user["id"]
        class_level = user["class_level"]
        if contains_personal_info(body):
            raise DBError("Please do not share phone numbers, emails or links in chat.")
    if not class_level:
        raise DBError("Choose a class first.")
    _rest("POST", "messages", json={
        "class_level": class_level, "thread_id": thread_id, "sender_id": user["id"],
        "sender_name": user["nickname"], "sender_role": role or "learner", "body": body,
    })


def list_broadcasts(class_level, limit=50):
    rows = _rest("GET", "messages", params={
        "class_level": f"eq.{class_level}", "thread_id": "is.null",
        "select": "id,sender_name,body,created_at", "order": "created_at.desc", "limit": str(limit),
    })
    return list(reversed(rows))


def list_thread(learner_id, limit=100):
    rows = _rest("GET", "messages", params={
        "thread_id": f"eq.{learner_id}",
        "select": "id,sender_name,sender_role,body,created_at", "order": "created_at.desc", "limit": str(limit),
    })
    return list(reversed(rows))


def delete_message(message_id):
    _rest("DELETE", "messages", params={"id": f"eq.{message_id}"})


def make_room_url(class_level, base_url=None):
    """An unguessable room name, so only people given the link can find it."""
    base = (base_url or VIDEO_BASE_URL).rstrip("/")
    slug = re.sub(r"[^A-Za-z0-9]", "", class_level or "Class")
    return f"{base}/LearnMate{slug}{secrets.token_hex(6)}"


def is_safe_meeting_url(url):
    return bool(re.fullmatch(r"https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~%/\-?=&#+]*)?", (url or "").strip()))


def start_session(user, class_level, learner_id=None, title=None, custom_url=None):
    if user.get("role") != "teacher":
        raise DBError("Only teachers can start a live session.")
    url = (custom_url or "").strip() or make_room_url(class_level)
    if not is_safe_meeting_url(url):
        raise DBError("The meeting link must start with https://")
    end_sessions(class_level, learner_id)
    _rest("POST", "live_sessions", json={
        "class_level": class_level, "learner_id": learner_id, "room_url": url,
        "title": (title or "").strip()[:80] or None, "started_by": user["id"],
    })
    return url


def end_sessions(class_level, learner_id=None):
    params = {"class_level": f"eq.{class_level}", "ended_at": "is.null"}
    params["learner_id"] = f"eq.{learner_id}" if learner_id else "is.null"
    _rest("PATCH", "live_sessions", params=params,
          json={"ended_at": datetime.now(timezone.utc).isoformat()})


def end_session(session_id):
    _rest("PATCH", "live_sessions", params={"id": f"eq.{session_id}"},
          json={"ended_at": datetime.now(timezone.utc).isoformat()})


def _fresh(rows):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SESSION_MAX_HOURS)
    return [r for r in rows if (_parse_time(r["created_at"]) or cutoff) >= cutoff]


def active_sessions_for(user):
    """Live sessions a learner may join: the whole class's, or one meant for them."""
    rows = _rest("GET", "live_sessions", params={
        "class_level": f"eq.{user['class_level']}", "ended_at": "is.null",
        "select": "id,learner_id,room_url,title,created_at", "order": "created_at.desc", "limit": "10",
    })
    return [r for r in _fresh(rows) if not r["learner_id"] or r["learner_id"] == user["id"]]


def active_sessions_all():
    rows = _rest("GET", "live_sessions", params={
        "ended_at": "is.null", "select": "id,class_level,learner_id,room_url,title,created_at",
        "order": "created_at.desc", "limit": "50",
    })
    return _fresh(rows)


def join_url(url, display_name):
    """Add the learner's nickname for Jitsi rooms; other providers get the plain link."""
    if "jit.si" in url or "8x8.vc" in url:
        from urllib.parse import quote
        return f'{url}#userInfo.displayName="{quote(display_name)}"'
    return url


# ----------------------------------------------------------------------------
# Syllabuses saved by teachers (override the files in the curriculums folder)
# ----------------------------------------------------------------------------
def list_syllabi():
    return _rest("GET", "syllabi", params={
        "select": "class_level,subject,body,updated_at", "order": "class_level.asc,subject.asc", "limit": "1000",
    })


def save_syllabus(user, class_level, subject, body):
    if user.get("role") != "teacher":
        raise DBError("Only teachers can change a syllabus.")
    body = (body or "").strip()
    if not class_level or not subject or not body:
        raise DBError("A class, a subject and some topics are needed.")
    _rest("POST", "syllabi", params={"on_conflict": "class_level,subject"}, json={
        "class_level": class_level, "subject": subject, "body": body[:20000],
        "updated_by": user["id"], "updated_at": datetime.now(timezone.utc).isoformat(),
    }, prefer="resolution=merge-duplicates")


def delete_syllabus(user, class_level, subject):
    if user.get("role") != "teacher":
        raise DBError("Only teachers can change a syllabus.")
    _rest("DELETE", "syllabi", params={"class_level": f"eq.{class_level}", "subject": f"eq.{subject}"})


# ----------------------------------------------------------------------------
# Plans, payments and daily limits
# ----------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _KAMPALA = _ZoneInfo("Africa/Kampala")
except Exception:  # pragma: no cover
    _KAMPALA = timezone(timedelta(hours=3))

# Prices are suggestions to test with parents. Change them here.
PLANS = {
    "month": {"label": "1 month", "days": 31, "price": 20000},
    "term":  {"label": "1 term (3 months)", "days": 92, "price": 50000},
    "year":  {"label": "1 year (3 terms)", "days": 365, "price": 135000},
}
SIBLING_DISCOUNT = 0.5
# None means no limit.
FREE_LIMITS = {"questions": 20, "quizzes": 2, "listens": 5}
PLUS_LIMITS = {"questions": 100, "quizzes": None, "listens": None}
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L


def today_kampala():
    return datetime.now(_KAMPALA).date().isoformat()


def plan_price(plan_code, second_child=False):
    price = PLANS[plan_code]["price"]
    return int(round(price * (SIBLING_DISCOUNT if second_child else 1) / 100.0) * 100)


def payment_config():
    """Where parents send mobile money. Set these in the app's secrets."""
    return {
        "mtn": os.getenv("MOMO_MTN_NUMBER", "").strip(),
        "airtel": os.getenv("MOMO_AIRTEL_NUMBER", "").strip(),
        "name": os.getenv("MOMO_ACCOUNT_NAME", "").strip(),
    }


def is_payment_admin(user):
    """Teachers can confirm payments. Set PAYMENT_ADMIN_NICKNAMES to limit it to certain teachers."""
    if not user or user.get("role") != "teacher":
        return False
    allowed = [n.strip().lower() for n in os.getenv("PAYMENT_ADMIN_NICKNAMES", "").split(",") if n.strip()]
    return not allowed or (user.get("nickname", "").lower() in allowed)


def _require_admin(user):
    if not is_payment_admin(user):
        raise DBError("You are not allowed to manage payments.")


def make_payment_code():
    return "LM-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(5))


def create_payment_request(user, plan_code, second_child=False):
    if user.get("role") != "learner":
        raise DBError("Only learners can ask for a payment code.")
    if plan_code not in PLANS:
        raise DBError("Choose a plan first.")
    # A learner keeps one open request at a time.
    _rest("DELETE", "payment_requests", params={"user_id": f"eq.{user['id']}", "status": "eq.pending"})
    code = make_payment_code()
    _rest("POST", "payment_requests", json={
        "user_id": user["id"], "nickname": user["nickname"], "class_level": user.get("class_level"),
        "plan_code": plan_code, "amount_ugx": plan_price(plan_code, second_child), "code": code,
    })
    return code


def my_payment_requests(user_id, limit=5):
    return _rest("GET", "payment_requests", params={
        "user_id": f"eq.{user_id}", "select": "id,plan_code,amount_ugx,code,txn_ref,status,note,created_at",
        "order": "created_at.desc", "limit": str(limit),
    })


def set_payment_reference(user_id, request_id, txn_ref):
    txn_ref = re.sub(r"[^A-Za-z0-9 _.-]", "", txn_ref or "").strip()[:40]
    _rest("PATCH", "payment_requests", params={"id": f"eq.{request_id}", "user_id": f"eq.{user_id}", "status": "eq.pending"},
          json={"txn_ref": txn_ref or None})


def list_payment_requests(status="pending", limit=200):
    return _rest("GET", "payment_requests", params={
        "status": f"eq.{status}",
        "select": "id,user_id,nickname,class_level,plan_code,amount_ugx,code,txn_ref,status,note,created_at,confirmed_at",
        "order": "created_at.desc", "limit": str(limit),
    })


def active_plan(user_id):
    """The learner's current paid plan, or None for the free plan."""
    now = datetime.now(timezone.utc).isoformat()
    rows = _rest("GET", "subscriptions", params={
        "user_id": f"eq.{user_id}", "ends_at": f"gt.{now}",
        "select": "plan_code,starts_at,ends_at,source", "order": "ends_at.desc", "limit": "1",
    })
    return rows[0] if rows else None


def _add_subscription(admin, user_row, plan_code, days, amount, source, reference):
    current = active_plan(user_row["user_id"])
    start = datetime.now(timezone.utc)
    if current:
        later = _parse_time(current["ends_at"])
        if later and later > start:
            start = later  # a new payment adds time after the current plan
    _rest("POST", "subscriptions", json={
        "user_id": user_row["user_id"], "nickname": user_row["nickname"], "class_level": user_row.get("class_level"),
        "plan_code": plan_code, "starts_at": start.isoformat(), "ends_at": (start + timedelta(days=days)).isoformat(),
        "amount_ugx": amount, "source": source, "reference": reference, "created_by": admin["id"],
    })


def confirm_payment(admin, request_id):
    _require_admin(admin)
    rows = _rest("GET", "payment_requests", params={"id": f"eq.{request_id}", "select": "*"})
    if not rows:
        raise DBError("That payment request was not found.")
    req = rows[0]
    if req["status"] != "pending":
        raise DBError("That payment was already handled.")
    _add_subscription(admin, {"user_id": req["user_id"], "nickname": req["nickname"], "class_level": req.get("class_level")},
                      req["plan_code"], PLANS[req["plan_code"]]["days"], req["amount_ugx"], "momo", req["code"])
    _rest("PATCH", "payment_requests", params={"id": f"eq.{request_id}"}, json={
        "status": "confirmed", "confirmed_at": datetime.now(timezone.utc).isoformat(), "confirmed_by": admin["id"]})


def reject_payment(admin, request_id, note=""):
    _require_admin(admin)
    _rest("PATCH", "payment_requests", params={"id": f"eq.{request_id}", "status": "eq.pending"},
          json={"status": "rejected", "note": (note or "")[:120] or None,
                "confirmed_at": datetime.now(timezone.utc).isoformat(), "confirmed_by": admin["id"]})


def grant_plan(admin, learners, days, reference):
    """School or sponsor plan: give Plus to several learners at once."""
    _require_admin(admin)
    days = max(1, min(int(days), 400))
    reference = (reference or "School or sponsor")[:80]
    for lr in learners:
        _add_subscription(admin, {"user_id": lr["id"], "nickname": lr["nickname"], "class_level": lr.get("class_level")},
                          "school", days, 0, "school", reference)


def list_subscriptions(limit=300):
    now = datetime.now(timezone.utc).isoformat()
    return _rest("GET", "subscriptions", params={
        "ends_at": f"gt.{now}", "select": "nickname,class_level,plan_code,starts_at,ends_at,amount_ugx,source,reference",
        "order": "ends_at.desc", "limit": str(limit),
    })


def confirmed_total(since_iso):
    rows = _rest("GET", "subscriptions", params={
        "source": "eq.momo", "created_at": f"gte.{since_iso}", "select": "amount_ugx", "limit": "5000"})
    return sum(int(r.get("amount_ugx") or 0) for r in rows)


def get_usage(user_id, day=None):
    day = day or today_kampala()
    rows = _rest("GET", "usage_daily", params={
        "user_id": f"eq.{user_id}", "day": f"eq.{day}", "select": "questions,quizzes,listens", "limit": "1"})
    base = {"questions": 0, "quizzes": 0, "listens": 0}
    if rows:
        base.update({k: int(rows[0].get(k) or 0) for k in base})
    return base


def add_usage(user_id, kind):
    if kind not in ("questions", "quizzes", "listens"):
        return
    day = today_kampala()
    use = get_usage(user_id, day)
    use[kind] += 1
    _rest("POST", "usage_daily", params={"on_conflict": "user_id,day"},
          json={"user_id": user_id, "day": day, **use}, prefer="resolution=merge-duplicates")


def limits_for(plan):
    return PLUS_LIMITS if plan else FREE_LIMITS
