import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "favourites.db")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS favourites (
                user_id INTEGER,
                stop_name TEXT,
                PRIMARY KEY (user_id, stop_name)
            )"""
        )


def get_favourites(user_id: int) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT stop_name FROM favourites WHERE user_id = ? ORDER BY stop_name",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows]


def is_favourite(user_id: int, stop_name: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM favourites WHERE user_id = ? AND stop_name = ?",
                (user_id, stop_name),
            ).fetchone()
            is not None
        )


def toggle_favourite(user_id: int, stop_name: str) -> bool:
    """Toggle favourite. Returns True if added, False if removed."""
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute(
            "SELECT 1 FROM favourites WHERE user_id = ? AND stop_name = ?",
            (user_id, stop_name),
        ).fetchone():
            conn.execute(
                "DELETE FROM favourites WHERE user_id = ? AND stop_name = ?",
                (user_id, stop_name),
            )
            return False
        conn.execute(
            "INSERT INTO favourites (user_id, stop_name) VALUES (?, ?)",
            (user_id, stop_name),
        )
        return True
