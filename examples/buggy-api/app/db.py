import sqlite3

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT UNIQUE, email TEXT, is_admin INTEGER DEFAULT 0);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, sku TEXT, quantity INTEGER, unit_price REAL);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users (name, email, is_admin) VALUES (?, ?, ?)",
        [("alice", "alice@example.com", 0), ("bob", "bob@example.com", 0), ("root", "root@example.com", 1)],
    )
    conn.commit()
