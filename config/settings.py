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
    ZROK_BINARY: str = "zrok2"
    ZROK_PRIVATE_TOKEN: str | None = None   # ZROK2_ADMIN_TOKEN from bootstrap
    ZROK_API_ENDPOINT: str | None = None    # e.g. http://127.0.0.1:18080
    ZROK_ACCOUNT_EMAIL: str = "admin@tele.local"
    ZROK_ACCOUNT_PASSWORD: str = "changeme123"
    # Bootstrap variables — only needed for /zrok_bootstrap command
    ZROK2_DNS_ZONE: str | None = None          # e.g. zrok.kyone.duckdns.org
    ZITI_ADMIN_PASSWORD: str | None = None     # OpenZiti admin password
    ZITI_API_ENDPOINT: str = "https://127.0.0.1:1280"
    ZROK2_TLS_CERT: str | None = None          # optional, e.g. /etc/letsencrypt/live/.../fullchain.pem
    ZROK2_TLS_KEY: str | None = None           # optional
    DUCKDNS_TOKEN: str | None = None
    DUCKDNS_DOMAIN: str | None = None          # just subdomain, e.g. "yourname"
    DUCKDNS_UPDATE_INTERVAL: int = 300         # seconds between auto IP updates
    # Comma-separated host paths to scan for git repos (on the WSL host, via SSH)
    # e.g. "/home/user,/home/user/bots,/mnt/d/code"
    # Leave blank to only scan the container's data/workspaces folder
    GIT_SCAN_PATHS: str | None = None

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
        
        return Settings(**doc)
    except Exception as ex:
        logging.critical(f"MongoDB target connection failed: {ex}. Dropping back safely to local environment vectors.")
        return base_env

runtime_settings = load_runtime_configs()