"""
TOTP 2FA gate for risky commands.

Usage
-----
1. User sends /verify <6-digit-code>
   → if valid, a session is opened for TOTP_SESSION_DURATION seconds (default 4h).

2. Wrap any risky handler with @require_2fa:

    from middlewares.totp_auth import require_2fa

    @router.message(Command("host"))
    @require_2fa
    async def cmd_host(message: Message):
        ...

Setup
-----
- TOTP_SECRET in .env  (base32, generated once)
- TOTP_SESSION_DURATION in .env (seconds, default 14400 = 4 hours)
- pip install pyotp
"""

import time
import logging
import functools
import pyotp

from aiogram.types import Message
from config.settings import runtime_settings

logger = logging.getLogger(__name__)

# ── Session store ─────────────────────────────────────────────────────────────
# { user_id: expiry_timestamp }
_sessions: dict[int, float] = {}


# ── Settings helpers — read at call time so changes apply without restart ──────

def _get_session_duration() -> int:
    """Return TOTP_SESSION_DURATION from settings (default 4 hours)."""
    return getattr(runtime_settings, "TOTP_SESSION_DURATION", 4 * 60 * 60)


def _session_duration_label() -> str:
    """Return human-readable session duration, e.g. '4 hours' or '1h 30m'."""
    secs = _get_session_duration()
    h = secs // 3600
    m = (secs % 3600) // 60
    if m == 0:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{h}h {m}m"


# ── Core helpers ──────────────────────────────────────────────────────────────

def _get_totp() -> pyotp.TOTP | None:
    secret = getattr(runtime_settings, "TOTP_SECRET", None)
    if not secret:
        logger.error("TOTP_SECRET is not set in .env — 2FA is disabled!")
        return None
    return pyotp.TOTP(secret)


def verify_code(code: str) -> bool:
    """Return True if the 6-digit TOTP code is currently valid."""
    totp = _get_totp()
    if totp is None:
        return False
    return totp.verify(code, valid_window=1)  # ±30s clock drift allowed


def open_session(user_id: int) -> float:
    """Open a session for user_id. Returns the expiry timestamp."""
    duration = _get_session_duration()
    expiry = time.time() + duration
    _sessions[user_id] = expiry
    logger.info(f"2FA session opened for user {user_id} — expires in {_session_duration_label()}")
    return expiry


def has_valid_session(user_id: int) -> bool:
    """Return True if the user has an active unexpired 2FA session."""
    expiry = _sessions.get(user_id)
    if expiry is None:
        return False
    if time.time() > expiry:
        del _sessions[user_id]
        return False
    return True


def session_time_left(user_id: int) -> str:
    """Human-readable time remaining in the current session."""
    expiry = _sessions.get(user_id)
    if not expiry:
        return "no active session"
    remaining = max(0, expiry - time.time())
    h = int(remaining // 3600)
    m = int((remaining % 3600) // 60)
    s = int(remaining % 60)
    return f"{h}h {m}m {s}s"


def revoke_session(user_id: int):
    """Manually revoke a session."""
    _sessions.pop(user_id, None)
    logger.info(f"2FA session revoked for user {user_id}")


# ── Decorator ─────────────────────────────────────────────────────────────────

def require_2fa(handler):
    """
    Decorator for aiogram message handlers.
    Blocks and prompts /verify if no valid 2FA session exists.
    """
    @functools.wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if not has_valid_session(message.from_user.id):
            await message.answer(
                f"🔐 <b>2FA Required</b>\n\n"
                f"This command is protected. Send your Google Authenticator code:\n"
                f"<code>/verify &lt;6-digit-code&gt;</code>\n\n"
                f"✅ A successful verification unlocks elevated commands for "
                f"<b>{_session_duration_label()}</b>.",
                parse_mode="HTML"
            )
            return
        return await handler(message, *args, **kwargs)
    return wrapper
