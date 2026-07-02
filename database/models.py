import os
import sqlite3
import logging
from config.settings import runtime_settings
from database.connection import get_db_connection

def init_db():
    """Initializes schema blueprints using the safe connection wrapper context."""
    try:
        os.makedirs(os.path.dirname(runtime_settings.DB_PATH), exist_ok=True)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    user_id INTEGER,
                    command TEXT,
                    status TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    name TEXT PRIMARY KEY,
                    repo_url TEXT,
                    status TEXT,
                    container_id TEXT,
                    last_deployed DATETIME DEFAULT CURRENT_TIMESTAMP,
                    health_status TEXT
                )
            ''')
            
            conn.commit()
            logging.info("SQLite storage initialization scheme successfully synced to disk.")
    except Exception as e:
        logging.critical(f"Failed to bootstrap database metadata schemas: {e}")
        raise e

def log_audit(user_id: int, command: str, status: str):
    """Inserts execution trail records safely wrapped inside context managers."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO audit_logs (user_id, command, status) VALUES (?, ?, ?)",
                (user_id, command, status)
            )
            conn.commit()
    except Exception as err:
        logging.error(f"Non-fatal tracking failure logging system audit transaction: {err}")