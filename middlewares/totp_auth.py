"""
TOTP 2FA gate for risky commands.

Usage
-----
1. User sends /verify <6-digit-code>
   → if valid, a 2-hour session is stored in memory.

2. Wrap any risky handler with @require_2fa:

    from middlewares.totp_auth import require_2fa

    @router.message(Command("host"))
    @require_2fa
    async def cmd_host(message: Message):
        ...

   If the session has expired the user gets a prompt to /verify again.

Setup
-----
- TOTP_SECRET in .env  (base32, generated once)
- pip install pyotp
- Scan the secret (or provisioning URI) into Google Authenticator manually,
  or generate a QR code uri:
      pyotp.totp.TOTP(secret).provisioning_uri("TeleDocker", issuer_name="YourBot")
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

SESSION_DURATION = 2 * 60 * 60   # 2 hours in seconds


# ── Core helpers ──────────────────────────────────────────────────────────────

def _get_totp() -> pyotp.TOTP | None:
    secret = runtime_settings.TOTP_SECRET
    if not secret:
        logger.error("TOTP_SECRET is not set in .env — 2FA is disabled!")
        return None
    return pyotp.TOTP(secret)


def verify_code(code: str) -> bool:
    """Return True if the 6-digit TOTP code is currently valid."""
    totp = _get_totp()
    if totp is None:
        return False
    # valid_window=1 allows ±30 s clock drift
    return totp.verify(code, valid_window=1)


def open_session(user_id: int) -> float:
    """Grant a 2-hour session for user_id. Returns the expiry timestamp."""
    expiry = time.time() + SESSION_DURATION
    _sessions[user_id] = expiry
    logger.info(f"2FA session opened for user {user_id}, expires in 2h")
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
    """Manually revoke a session (e.g. /lock command)."""
    _sessions.pop(user_id, None)
    logger.info(f"2FA session revoked for user {user_id}")


# ── Decorator ─────────────────────────────────────────────────────────────────

def require_2fa(handler):
    """
    Decorator for aiogram message handlers.
    Blocks execution and prompts /verify if no valid 2FA session exists.
    """
    @functools.wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if not has_valid_session(message.from_user.id):
            await message.answer(
                "🔐 <b>2FA Required</b>\n\n"
                "This command is protected. Send your Google Authenticator code:\n"
                "<code>/verify &lt;6-digit-code&gt;</code>\n\n"
                "✅ A successful verification unlocks elevated commands for <b>2 hours</b>.",
                parse_mode="HTML"
            )
            return
        return await handler(message, *args, **kwargs)
    return wrapper
