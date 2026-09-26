import json

from app.api import handle
from app.db import connect, seed


def test_get_user():
    conn = connect()
    seed(conn)
    status, body = handle(conn, "GET", "/users/alice", {})
    assert status == 200
    assert json.loads(body)["email"] == "alice@example.com"
