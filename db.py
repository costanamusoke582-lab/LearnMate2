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
