"""
Persistent registry for Python deployments managed via /pydeploy and /pyps.

Lives in its own table (python_deployments) inside the same SQLite database
used by the rest of the bot (database/connection.py). This module is purely
additive: it does not read, modify, or depend on any existing table, and
nothing in database/models.py is touched.
"""

import logging
from database.connection import get_db_connection

logger = logging.getLogger(__name__)


def init_pydeploy_db():
    """Create the python_deployments table if it doesn't exist yet."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS python_deployments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    source_type TEXT NOT NULL,
                    source TEXT,
                    path TEXT NOT NULL,
                    venv_path TEXT NOT NULL,
                    entry_point TEXT NOT NULL,
                    pid INTEGER,
                    desired_state TEXT NOT NULL DEFAULT 'stopped',
                    last_status TEXT NOT NULL DEFAULT 'stopped',
                    restart_count INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

            # Additive migration for existing rows created before custom
            # build/run commands and inline .env content were supported.
            existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(python_deployments)").fetchall()}
            for col in ("build_command", "run_command", "env_content"):
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE python_deployments ADD COLUMN {col} TEXT")
            conn.commit()
            logger.info("python_deployments table ready.")
    except Exception as e:
        logger.error(f"Failed to initialize python_deployments table: {e}")
        raise e


def create_deployment(
    name: str, source_type: str, source: str, path: str, venv_path: str, entry_point: str,
    build_command: str = None, run_command: str = None, env_content: str = None,
) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO python_deployments
               (name, source_type, source, path, venv_path, entry_point,
                build_command, run_command, env_content, desired_state, last_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'stopped', 'deploying')''',
            (name, source_type, source, path, venv_path, entry_point or "", build_command, run_command, env_content)
        )
        conn.commit()
        return cursor.lastrowid


def update_deployment(deployment_id: int, **fields):
    """Update arbitrary columns on a deployment row. No-op if fields is empty."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [deployment_id]
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE python_deployments SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            values
        )
        conn.commit()


def get_deployment(deployment_id: int) -> dict | None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT * FROM python_deployments WHERE id = ?", (deployment_id,)).fetchone()
        return dict(row) if row else None


def get_deployment_by_name(name: str) -> dict | None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT * FROM python_deployments WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def delete_deployment(deployment_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM python_deployments WHERE id = ?", (deployment_id,))
        conn.commit()


def list_deployments() -> list[dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM python_deployments ORDER BY name ASC").fetchall()
        return [dict(r) for r in rows]
