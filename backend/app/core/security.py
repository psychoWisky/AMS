import secrets
import string
from datetime import datetime, timezone, timedelta
from typing import Optional
from jose import jwt, JWTError
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)

def generate_temp_password(length: int = 10) -> str:
    """Cryptographically secure temporary credential (e.g. Orientation account
    creation). Uses `secrets`, not `random`, unlike this project's existing
    OTP generators — deliberately not repeating that known weakness here."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def _make_token(data: dict, expires_delta: timedelta) -> str:
    payload = {**data, "exp": datetime.now(timezone.utc) + expires_delta}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

def create_access_token(subject: str, extra_claims: dict | None = None) -> str:
    data = {"sub": subject, "type": "access", **(extra_claims or {})}
    return _make_token(data, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))

def create_refresh_token(subject: str) -> str:
    return _make_token({"sub": subject, "type": "refresh"},
                       timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))

def create_bulk_upload_confirmation_token(subject: str, file_hash: str, warning_count: int) -> str:
    """Course bulk-upload duplicate-warning confirmation fix — a short-lived,
    HMAC-signed (same SECRET_KEY/algorithm as access/refresh tokens) token
    proving a specific user was actually shown a specific, byte-identical
    file's duplicate-code warnings before confirming. `file_hash` (sha256 of
    the raw uploaded bytes) is the anti-tamper binding: the confirm step
    re-hashes whatever file is re-submitted and rejects a mismatch outright,
    so a client can never preview file A's (mild) warnings and then use the
    resulting token to silently push through a completely different file B.
    `warning_count` is carried only for a cheap sanity check, not itself a
    security boundary. Contains no course/user PII beyond the user's own id
    (`sub`) they are already authenticated as."""
    data = {"sub": subject, "type": "course_bulk_upload_confirm", "file_hash": file_hash, "warning_count": warning_count}
    return _make_token(data, timedelta(minutes=15))

def decode_token(token: str, token_type: str = "access") -> Optional[dict]:
    """Multi-role/role-switching task — returns the full decoded payload
    (not just `sub`) so callers can also read the `sid` session-pointer claim
    (see create_access_token). Like `sub`, `sid` is never trusted as a value
    in itself — it is only a lookup key into a fresh, server-side DB row
    (app.core.dependencies.get_current_user); a forged `sid` requires a
    validly-signed token in the first place, the same trust boundary `sub`
    already relies on."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != token_type:
            return None
        return payload
    except JWTError:
        return None


def verify_token(token: str, token_type: str = "access") -> Optional[str]:
    payload = decode_token(token, token_type)
    return payload.get("sub") if payload else None
