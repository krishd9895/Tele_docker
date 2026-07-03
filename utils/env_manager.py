"""
EnvManager — persistent mutable settings store.

Architecture:
  - .env is mounted read-only — never written by the bot
  - Mutable non-sensitive settings are stored in:
      1. MongoDB  `tg_docker_manager.bot_settings`  (if MONGO_URI is set)
      2. data/settings.json                          (fallback, always available)
  - On startup, base settings load from .env, then this store overlays on top
  - Sensitive keys (tokens, passwords) are NEVER stored here

Safe to store here:
  DUCKDNS_DOMAIN, DUCKDNS_UPDATE_INTERVAL, GIT_SCAN_PATHS,
  DEPLOYMENT_TIMEOUT, ZROK_API_ENDPOINT, etc.

NOT stored here (stays in .env only):
  TELEGRAM_BOT_TOKEN, RESCUE_BOT_TOKEN, TOTP_SECRET,
  HOST_SSH_PASSWORD, DUCKDNS_TOKEN, ALLOWED_USER_ID
"""

import json
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SETTINGS_JSON_PATH = Path("data/settings.json")

# Keys that must NEVER be stored in the mutable store
_SENSITIVE_KEYS = {
    "TELEGRAM_BOT_TOKEN",
    "RESCUE_BOT_TOKEN",
    "TOTP_SECRET",
    "HOST_SSH_PASSWORD",
    "SECRET_ADMIN_PASSWORD",
    "DUCKDNS_TOKEN",          # token is sensitive, domain is not
    "ALLOWED_USER_ID",         # security-critical
    "MONGO_URI",               # contains credentials
}

# Keys shown masked in /showenv
_MASKED_DISPLAY = {"HOST_SSH_USER"}


def _is_sensitive(key: str) -> bool:
    return key.upper() in _SENSITIVE_KEYS


# ── MongoDB helpers ───────────────────────────────────────────────────────────

def _get_mongo_collection():
    """Return MongoDB collection or None if not configured."""
    try:
        from config.settings import runtime_settings
        if not runtime_settings.MONGO_URI:
            return None
        from pymongo import MongoClient
        client = MongoClient(runtime_settings.MONGO_URI, serverSelectionTimeoutMS=2000)
        return client["tg_docker_manager"]["bot_settings"]
    except Exception:
        return None


def _load_from_mongo() -> dict:
    col = _get_mongo_collection()
    if col is None:
        return {}
    try:
        doc = col.find_one({"_id": "mutable_settings"}, {"_id": 0})
        return dict(doc) if doc else {}
    except Exception as e:
        logger.warning(f"Could not load settings from MongoDB: {e}")
        return {}


def _save_to_mongo(settings: dict) -> bool:
    col = _get_mongo_collection()
    if col is None:
        return False
    try:
        col.update_one(
            {"_id": "mutable_settings"},
            {"$set": settings},
            upsert=True
        )
        return True
    except Exception as e:
        logger.warning(f"Could not save settings to MongoDB: {e}")
        return False


# ── JSON fallback ─────────────────────────────────────────────────────────────

def _load_from_json() -> dict:
    if not SETTINGS_JSON_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not load settings.json: {e}")
        return {}


def _save_to_json(settings: dict) -> bool:
    try:
        SETTINGS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_JSON_PATH.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        return True
    except Exception as e:
        logger.error(f"Could not save settings.json: {e}")
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def load_mutable_settings() -> dict:
    """
    Load mutable settings. Tries MongoDB first, falls back to JSON.
    Returns a dict of key → value for non-sensitive settings.
    """
    # MongoDB takes priority
    mongo_data = _load_from_mongo()
    if mongo_data:
        logger.info(f"Loaded {len(mongo_data)} mutable settings from MongoDB.")
        return mongo_data

    # JSON fallback
    json_data = _load_from_json()
    if json_data:
        logger.info(f"Loaded {len(json_data)} mutable settings from settings.json.")
    return json_data


def save_setting(key: str, value: str) -> tuple[bool, str]:
    """
    Save a single mutable setting.
    Returns (True, backend_used) or (False, error_message).
    """
    key = key.strip().upper()
    value = str(value).strip()

    if _is_sensitive(key):
        return False, f"{key} is a sensitive key and cannot be stored here."

    # Load current, update, save
    current = load_mutable_settings()
    current[key] = value

    # Try MongoDB first
    if _save_to_mongo(current):
        logger.info(f"Setting saved to MongoDB: {key}")
        return True, "mongodb"

    # Fallback to JSON
    if _save_to_json(current):
        logger.info(f"Setting saved to settings.json: {key}")
        return True, "json"

    return False, "Both MongoDB and settings.json write failed."


def save_settings(pairs: dict) -> tuple[bool, str]:
    """Save multiple settings at once."""
    # Filter sensitive keys
    safe = {k.upper(): v for k, v in pairs.items() if not _is_sensitive(k.upper())}
    if not safe:
        return False, "All provided keys are sensitive — nothing to save."

    current = load_mutable_settings()
    current.update(safe)

    if _save_to_mongo(current):
        return True, "mongodb"
    if _save_to_json(current):
        return True, "json"
    return False, "Write failed to all backends."


def get_all_settings() -> dict:
    """Get all mutable stored settings."""
    return load_mutable_settings()


def apply_to_runtime(settings: dict) -> None:
    """
    Overlay mutable settings onto runtime_settings in memory.
    Called at startup and after each save.
    """
    try:
        from config.settings import runtime_settings
        for key, value in settings.items():
            if hasattr(runtime_settings, key) and not _is_sensitive(key):
                try:
                    field_type = type(getattr(runtime_settings, key))
                    if field_type == int:
                        setattr(runtime_settings, key, int(value))
                    elif field_type == bool:
                        setattr(runtime_settings, key, str(value).lower() in ("true", "1", "yes"))
                    else:
                        setattr(runtime_settings, key, value)
                    logger.debug(f"Runtime overlay: {key}")
                except Exception as e:
                    logger.warning(f"Could not apply setting {key}: {e}")
    except Exception as e:
        logger.error(f"apply_to_runtime failed: {e}")


def mask_value(key: str, value: str) -> str:
    """Return display-safe value for a setting."""
    key = key.upper()
    if key in _SENSITIVE_KEYS:
        return "••••••••"
    if key in _MASKED_DISPLAY:
        return value[:2] + "***" if len(value) > 2 else "***"
    return value
