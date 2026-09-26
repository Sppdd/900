"""Tiny request router (framework-free so the demo needs no dependencies)."""

import json

from app.pricing import order_total
from app.users import find_user, search_users


def handle(conn, method: str, path: str, query: dict[str, str], body: str = "") -> tuple[int, str]:
    if method == "GET" and path == "/users":
        return 200, json.dumps(search_users(conn, query.get("prefix", "")))
    if method == "GET" and path.startswith("/users/"):
        user = find_user(conn, path.removeprefix("/users/"))
        return (200, json.dumps(user)) if user else (404, '{"error": "not found"}')
    if method == "POST" and path == "/quote":
        data = json.loads(body or "{}")
        lines = [(int(line["quantity"]), float(line["unit_price"])) for line in data.get("lines", [])]
        return 200, json.dumps({"total": order_total(lines, data.get("coupon"))})
    return 404, '{"error": "no route"}'
