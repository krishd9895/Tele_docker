import os
import logging
from pydantic_settings import BaseSettings
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str = "placeholder"
    ALLOWED_USER_ID: int = 0
    SECRET_ADMIN_PASSWORD: str | None = None
    MONGO_URI: str | None = None
    DB_PATH: str = "data/manager.db"
    DEPLOYMENT_TIMEOUT: int = 600
    TOTP_SECRET: str | None = None
    DUCKDNS_TOKEN: str | None = None
    DUCKDNS_DOMAIN: str | None = None
    DUCKDNS_UPDATE_INTERVAL: int = 300
    GIT_SCAN_PATHS: str | None = None
    CF_TUNNEL_TOKEN: str | None = None   # Cloudflare named tunnel token for permanent URLs
    # Host SSH settings
    HOST_SSH_USER: str | None = None
    HOST_SSH_PASSWORD: str | None = None
    HOST_SSH_HOST: str = "127.0.0.1"
    HOST_SSH_PORT: int = 22
    # File browser settings
    BROWSER_ROOT: str = "/home/d"
    # 2FA settings
    TOTP_SESSION_DURATION: int = 4 * 60 * 60  # 4 hours in seconds

    class Config:
        env_file = ".env"
        extra = "ignore"

def load_runtime_configs() -> Settings:
    base_env = Settings()
    if not base_env.MONGO_URI or "optional" in base_env.MONGO_URI:
        logging.warning("No MongoDB URI provided or default detected. Running natively off environment variables.")
        return base_env
    
    try:
        client = MongoClient(base_env.MONGO_URI, serverSelectionTimeoutMS=3000)
        db = client["tg_docker_manager"]
        config_collection = db["runtime_settings"]
        
        doc = config_collection.find_one({"config_id": "global_production"})
        if not doc:
            initial_payload = base_env.model_dump()
            initial_payload["config_id"] = "global_production"
            config_collection.insert_one(initial_payload)
            return base_env
        
        # Merge: start with all current defaults, then overlay what's in MongoDB.
        # This means new fields added to Settings always get their default value
        # even if the stored MongoDB doc predates them.
        base_dict = base_env.model_dump()
        doc.pop("_id", None)
        doc.pop("config_id", None)
        # Only overlay keys that still exist in the current Settings model
        valid_keys = set(base_dict.keys())
        merged = {**base_dict, **{k: v for k, v in doc.items() if k in valid_keys}}
        return Settings(**merged)

    except Exception as ex:
        logging.critical(f"MongoDB target connection failed: {ex}. Dropping back safely to local environment vectors.")
        return base_env

runtime_settings = load_runtime_configs()