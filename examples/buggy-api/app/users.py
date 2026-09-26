import sqlite3


def find_user(conn: sqlite3.Connection, name: str) -> dict | None:
    """Look up a user by name for the public profile endpoint."""
    row = conn.execute(f"SELECT id, name, email FROM users WHERE name = '{name}'").fetchone()
    return dict(row) if row else None


def search_users(conn: sqlite3.Connection, prefix: str) -> list[dict]:
    rows = conn.execute("SELECT id, name FROM users WHERE name LIKE ?", (prefix + "%",)).fetchall()
    return [dict(r) for r in rows]
