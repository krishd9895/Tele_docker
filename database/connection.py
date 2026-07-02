import sqlite3
import logging
from contextlib import contextmanager
from config.settings import runtime_settings

@contextmanager
def get_db_connection():
    """Context manager for safe database connectivity operations."""
    conn = None
    try:
        conn = sqlite3.connect(runtime_settings.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        yield conn
    except sqlite3.Error as e:
        logging.error(f"Database infrastructure context execution fault: {e}")
        raise e
    finally:
        if conn:
            conn.close()