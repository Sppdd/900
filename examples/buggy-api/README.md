# buggy-api (demo project)

A deliberately flawed mini API used to demo the sandcoder panel. It contains:

- a SQL injection in `app/users.py` (for security-auditor)
- almost no tests (for test-writer)
- an off-by-one pricing bug described in `BUG_REPORT.md` (for bug-reproducer)
- no CSV export yet (for feature-researcher: "add CSV export of orders")

Tests: `python -m pytest -q`
