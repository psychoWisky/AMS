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

def verify_token(token: str, token_type: str = "access") -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != token_type:
            return None
        return payload.get("sub")
    except JWTError:
        return None
