"""Authentication, sessions, and tenant metadata for the WebUI.

The authentication database contains no account-pool credentials.  Each user
gets a separate SQLite database under ``USER_DATA_DIR``; request middleware
selects that database after the session cookie has been verified.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


WEBUI_DIR = Path(__file__).resolve().parent
AUTH_DB_PATH = Path(os.getenv("GPT_WEBUI_AUTH_DB", str(WEBUI_DIR / "auth.db"))).expanduser().resolve()
USER_DATA_DIR = Path(os.getenv("GPT_WEBUI_USER_DATA_DIR", str(WEBUI_DIR / "user_data"))).expanduser().resolve()
SESSION_COOKIE = "webui_session"
CSRF_COOKIE = "webui_csrf"
SESSION_TTL_SECONDS = 8 * 60 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{2,63}$")
ROLES = frozenset({"admin", "user"})


class AuthError(Exception):
    """Base class for safe, user-facing authentication errors."""


class ValidationError(AuthError):
    pass


class AuthenticationError(AuthError):
    pass


class AuthorizationError(AuthError):
    pass


_LOCK = __import__("threading").RLock()


def _conn() -> sqlite3.Connection:
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(AUTH_DB_PATH), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_auth_db() -> None:
    """Create the control database and opportunistically bootstrap env admin."""
    with _LOCK:
        con = _conn()
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id              TEXT PRIMARY KEY,
                username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash   TEXT NOT NULL,
                role            TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                active          INTEGER NOT NULL DEFAULT 1,
                db_path         TEXT NOT NULL UNIQUE,
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash      TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_hash       TEXT NOT NULL,
                created_at      REAL NOT NULL,
                expires_at      REAL NOT NULL,
                last_seen_at    REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
            """
        )
        con.commit()
        con.close()
    bootstrap_from_environment()


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    password = str(password or "")
    if len(password) < 12 or len(password) > 256:
        raise ValidationError("Password must be between 12 and 256 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


_DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$00000000000000000000000000000000$"
    + hashlib.scrypt(
        b"not-a-real-password", salt=b"\0" * 16, n=2**14, r=8, p=1, dklen=32
    ).hex()
)


def verify_password(password: str, user: dict[str, Any]) -> bool:
    encoded = str(user.get("password_hash") or "")
    if not encoded and user.get("id"):
        con = _conn()
        try:
            row = con.execute(
                "SELECT password_hash FROM users WHERE id=?", (str(user["id"]),)
            ).fetchone()
            encoded = str(row["password_hash"] if row else "")
        finally:
            con.close()
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        _, n_text, r_text, p_text, salt_hex, digest_hex = parts
    except (IndexError, ValueError):
        return False
    try:
        if int(n_text) != 2**14 or int(r_text) != 8 or int(p_text) != 1:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(expected) != 32:
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=salt,
            n=int(n_text), r=int(r_text), p=int(p_text), dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def _public_user(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    # Password hashes and internal session fields never leave this module.
    return {
        "id": str(data["id"]),
        "username": str(data["username"]),
        "role": str(data["role"]),
        "active": bool(data["active"]),
        "db_path": str(data["db_path"]),
        "created_at": float(data["created_at"]),
    }


def _user_row(username: str) -> sqlite3.Row | None:
    con = _conn()
    try:
        return con.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    finally:
        con.close()


def get_user(user_id: str) -> dict[str, Any] | None:
    con = _conn()
    try:
        row = con.execute("SELECT * FROM users WHERE id=?", (str(user_id),)).fetchone()
        return _public_user(row)
    finally:
        con.close()


def create_user(username: str, password: str, *, role: str = "user") -> dict[str, Any]:
    username = str(username or "").strip()
    role = str(role or "user").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValidationError("Username must be 3-64 characters using letters, numbers, dot, underscore, or hyphen")
    if role not in ROLES:
        raise ValidationError("Role must be admin or user")
    password_hash = hash_password(password)
    user_id = uuid.uuid4().hex
    now = time.time()
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    db_path = str((USER_DATA_DIR / f"{user_id}.db").resolve())
    is_first_user = False
    with _LOCK:
        con = _conn()
        try:
            is_first_user = con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            con.execute(
                "INSERT INTO users(id, username, password_hash, role, active, db_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (user_id, username, password_hash, role, db_path, now, now),
            )
            con.commit()
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ValidationError("Username is already in use") from exc
        finally:
            con.close()
    try:
        _initialize_user_database(db_path, migrate_legacy=is_first_user)
    except Exception as exc:
        with _LOCK:
            con = _conn()
            con.execute("DELETE FROM users WHERE id=?", (user_id,))
            con.commit()
            con.close()
        raise AuthError("Unable to initialize the user database") from exc
    with _LOCK:
        con = _conn()
        con.execute("UPDATE users SET active=1, updated_at=? WHERE id=?", (time.time(), user_id))
        con.commit()
        con.close()
    user = get_user(user_id)
    assert user is not None
    return user


def create_first_admin(username: str, password: str) -> dict[str, Any]:
    """Atomically create the one-time initial administrator."""
    with _LOCK:
        if user_count() != 0:
            raise ValidationError("Initial administrator has already been configured")
        return create_user(username, password, role="admin")


def _initialize_user_database(path: str, *, migrate_legacy: bool = False) -> None:
    from . import db

    target = Path(path)
    legacy_path = Path(db.DB_PATH).resolve()
    if not target.exists() and migrate_legacy and legacy_path.exists():
        db.backup_database(legacy_path, target)
    with db.use_database_path(target):
        db.init_db()


def list_users() -> list[dict[str, Any]]:
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [_public_user(row) for row in rows]
    finally:
        con.close()


def can_manage_users(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("active") and user.get("role") == "admin")


def delete_user(user_id: str, *, actor: dict[str, Any]) -> None:
    if not can_manage_users(actor):
        raise AuthorizationError("Administrator permission required")
    if str(user_id) == str(actor.get("id")):
        raise AuthorizationError("You cannot disable your own account")
    with _LOCK:
        con = _conn()
        try:
            row = con.execute("SELECT role FROM users WHERE id=? AND active=1", (str(user_id),)).fetchone()
            if row is None:
                raise ValidationError("User not found")
            if row["role"] == "admin":
                remaining = con.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                    (str(user_id),),
                ).fetchone()[0]
                if not remaining:
                    raise AuthorizationError("At least one active administrator is required")
            con.execute("UPDATE users SET active=0, updated_at=? WHERE id=?", (time.time(), str(user_id)))
            con.execute("DELETE FROM sessions WHERE user_id=?", (str(user_id),))
            con.commit()
        finally:
            con.close()


def set_user_active(user_id: str, active: bool, *, actor: dict[str, Any]) -> dict[str, Any]:
    if not can_manage_users(actor):
        raise AuthorizationError("Administrator permission required")
    if not active:
        delete_user(user_id, actor=actor)
    else:
        with _LOCK:
            con = _conn()
            row = con.execute("SELECT 1 FROM users WHERE id=?", (str(user_id),)).fetchone()
            if row is None:
                con.close()
                raise ValidationError("User not found")
            con.execute("UPDATE users SET active=1, updated_at=? WHERE id=?", (time.time(), str(user_id)))
            con.commit()
            con.close()
    user = get_user(user_id)
    assert user is not None
    return user


def reset_password(user_id: str, password: str, *, actor: dict[str, Any]) -> None:
    if not can_manage_users(actor):
        raise AuthorizationError("Administrator permission required")
    encoded = hash_password(password)
    with _LOCK:
        con = _conn()
        cur = con.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (encoded, time.time(), str(user_id)),
        )
        if cur.rowcount != 1:
            con.close()
            raise ValidationError("User not found")
        con.execute("DELETE FROM sessions WHERE user_id=?", (str(user_id),))
        con.commit()
        con.close()


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    row = _user_row(str(username or ""))
    if row is None:
        # Perform equivalent password work so a remote caller cannot enumerate
        # usernames using response timing.
        verify_password(password, {"password_hash": _DUMMY_PASSWORD_HASH})
        return None
    if not bool(row["active"]):
        verify_password(password, dict(row))
        return None
    user = dict(row)
    return _public_user(row) if verify_password(password, user) else None


def create_session(user_id: str, *, ttl_seconds: int = SESSION_TTL_SECONDS) -> dict[str, Any]:
    user = get_user(user_id)
    if user is None or not user["active"]:
        raise AuthenticationError("User is not active")
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        con = _conn()
        con.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf_hash, created_at, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_hash_token(token), user_id, _hash_token(csrf), now, now + max(300, int(ttl_seconds)), now),
        )
        con.commit()
        con.close()
    return {"token": token, "csrf": csrf, "user": user}


def get_user_by_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    now = time.time()
    con = _conn()
    try:
        row = con.execute(
            "SELECT u.*, s.csrf_hash FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
            (_hash_token(token), now),
        ).fetchone()
        if row is None:
            return None
        con.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, _hash_token(token)))
        con.commit()
        user = _public_user(row)
        assert user is not None
        user["_csrf_hash"] = str(row["csrf_hash"])
        return user
    finally:
        con.close()


def verify_csrf(user: dict[str, Any], token: str) -> bool:
    expected = str(user.get("_csrf_hash") or "")
    return bool(expected and token and hmac.compare_digest(expected, _hash_token(token)))


def revoke_session(token: str) -> None:
    if not token:
        return
    with _LOCK:
        con = _conn()
        con.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        con.commit()
        con.close()


def bootstrap_from_environment() -> dict[str, Any] | None:
    """Create the first admin only when both explicit env values are present."""
    con = _conn()
    try:
        has_user = con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        con.close()
    if has_user:
        return None
    username = os.getenv("GPT_WEBUI_ADMIN_USERNAME", "").strip()
    password = os.getenv("GPT_WEBUI_ADMIN_PASSWORD", "")
    if not username or not password:
        return None
    try:
        return create_first_admin(username, password)
    except ValidationError:
        return None


def user_count() -> int:
    con = _conn()
    try:
        return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    finally:
        con.close()


def cleanup_expired_sessions() -> int:
    with _LOCK:
        con = _conn()
        cur = con.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        con.commit()
        n = cur.rowcount
        con.close()
        return n
