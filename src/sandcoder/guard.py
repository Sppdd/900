"""Guard layer: screens agent commands before they run and patches before they're returned.

This is defense in depth, not the security boundary. The boundary is isolation:
sandboxes hold no secrets, and model/API calls run outside them. The guard
catches common prompt-injection payloads (exfiltration, remote code, env dumps)
early, and gives the host agent a risk rating for every patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (rule id, pattern, reason). Matched case-insensitively against the full command.
DENY_RULES: list[tuple[str, str, str]] = [
    ("pipe-to-shell", r"(curl|wget)[^|;&]*\|\s*(ba|z)?sh\b", "downloads and executes remote code"),
    ("remote-exec", r"\b(bash|sh|python3?)\s+<\(\s*(curl|wget)", "executes remote code"),
    ("ssh-keys", r"(~|\$HOME|/root|/home/[^/\s]+)/\.ssh\b", "reads SSH material"),
    ("env-dump", r"(^|[;&|]\s*)(env|printenv|set)\s*($|[;&|>])|/proc/[^\s]*/environ", "dumps environment variables"),
    ("cloud-metadata", r"169\.254\.169\.254|metadata\.google|metadata\.internal", "queries cloud metadata"),
    ("reverse-shell", r"/dev/tcp/|\bnc\b[^;&|]*\s-e\s|\bncat\b[^;&|]*--exec|socat[^;&|]*exec", "opens a reverse shell"),
    ("miner", r"xmrig|minerd|cpuminer|stratum\+tcp", "runs a crypto miner"),
    ("wipe-root", r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|/\*|~)(\s|$)", "wipes the filesystem"),
    ("fork-bomb", r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    ("b64-exec", r"base64\s+(-d|--decode)[^|]*\|\s*(ba|z)?sh|eval\s*\(\s*base64", "executes obfuscated code"),
]

# Destinations the agent may legitimately reach (package indexes, VCS hosts).
NETWORK_ALLOW = (
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com",
    "objects.githubusercontent.com", "raw.githubusercontent.com", "crates.io", "proxy.golang.org",
    "localhost", "127.0.0.1",
)
URL_RE = re.compile(r"https?://([a-z0-9.\-]+)", re.I)


@dataclass
class Verdict:
    allowed: bool
    rule: str = ""
    reason: str = ""


def check_command(command: str) -> Verdict:
    """Allow or deny one shell command before it reaches the sandbox."""
    for rule, pattern, reason in DENY_RULES:
        if re.search(pattern, command, re.I | re.M):
            return Verdict(False, rule, reason)
    if re.search(r"\b(curl|wget|http(ie)?|nc|ncat)\b", command, re.I):
        for host in URL_RE.findall(command):
            if not any(host == h or host.endswith("." + h) for h in NETWORK_ALLOW):
                return Verdict(False, "network-egress", f"network access to non-allow-listed host {host}")
    return Verdict(True)


@dataclass
class PatchReport:
    risk: str = "low"  # low | medium | high
    reasons: list[str] = field(default_factory=list)

    def flag(self, level: str, reason: str) -> None:
        order = {"low": 0, "medium": 1, "high": 2}
        if order[level] > order[self.risk]:
            self.risk = level
        self.reasons.append(reason)


DEP_FILES = ("requirements", "pyproject.toml", "package.json", "setup.py", "setup.cfg", "Pipfile", "go.mod", "Cargo.toml")
CI_PATHS = (".github/", ".gitlab-ci", ".circleci/", "Jenkinsfile", ".pre-commit-config", "Makefile", "Dockerfile")


def scan_patch(diff: str) -> PatchReport:
    """Rate a unified diff for risky changes the host agent should look at before applying."""
    report = PatchReport()
    current = ""
    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("--- ") or line.startswith("diff --git"):
            continue
        elif line.startswith("+"):
            added.append((current, line[1:]))
        elif line.startswith("-"):
            removed.append((current, line[1:]))

    files = {f for f, _ in added + removed}
    for f in sorted(files):
        name = f.rsplit("/", 1)[-1]
        if any(f.startswith(p) or name.startswith(p) for p in CI_PATHS):
            report.flag("high", f"modifies CI/build config: {f}")
        if any(name.startswith(p) for p in DEP_FILES):
            report.flag("medium", f"changes dependencies: {f}")

    for f, text in added:
        for rule, pattern, reason in DENY_RULES:
            if re.search(pattern, text, re.I):
                report.flag("high", f"{f}: adds code that {reason} ({rule})")
        if re.search(r"\b(eval|exec)\s*\(|__import__\(|pickle\.loads|marshal\.loads", text):
            report.flag("medium", f"{f}: adds dynamic code execution")
        if re.search(r"(requests|httpx|urllib\.request|aiohttp|socket)\.\w+\(", text):
            report.flag("medium", f"{f}: adds network calls")
        if re.search(r"[A-Za-z0-9+/]{120,}={0,2}", text):
            report.flag("medium", f"{f}: adds a long encoded blob")
        if _is_test(f) and re.search(r"@(pytest\.mark\.)?skip|unittest\.skip|\.skipTest\(", text):
            report.flag("high", f"{f}: skips tests")

    removed_asserts = sum(1 for f, t in removed if _is_test(f) and re.search(r"\bassert", t))
    added_asserts = sum(1 for f, t in added if _is_test(f) and re.search(r"\bassert", t))
    if removed_asserts > added_asserts:
        report.flag("high", f"weakens tests: removes {removed_asserts - added_asserts} more asserts than it adds")
    return report


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}"
