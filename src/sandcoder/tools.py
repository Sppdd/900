"""Tools the model can call. Each call runs against the sandbox and may advance the snapshot."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sandcoder import guard
from sandcoder.sandbox import Sandbox, SandboxError, Snapshot

MAX_TOOL_OUTPUT = 12_000


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """Keep head and tail of long output; the tail usually holds the error."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars omitted] ...\n{text[-half:]}"


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn(
        "bash",
        "Run a shell command in the workspace (cwd is the project root). Filesystem changes persist. "
        "Non-interactive only; no sudo prompts, no long-running servers.",
        {
            "command": {"type": "string"},
            "timeout": {"type": "number", "description": "Seconds, default 120."},
        },
        ["command"],
    ),
    _fn(
        "read_file",
        "Read a text file (path relative to the project root). Returns numbered lines.",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based, inclusive."},
            "end_line": {"type": "integer", "description": "1-based, inclusive."},
        },
        ["path"],
    ),
    _fn(
        "write_file",
        "Create or overwrite a file with the full given content.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _fn(
        "edit_file",
        "Replace one exact, unique occurrence of old_text with new_text in a file.",
        {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"],
    ),
    _fn(
        "run_tests",
        "Run the project's test command. Does not modify the workspace. Returns exit code and output.",
        {},
        [],
    ),
    _fn(
        "checkpoint",
        "Save the current workspace state under a name so you can return to it later.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _fn(
        "rollback",
        "Restore the workspace to a named checkpoint ('start' is the initial state). "
        "Use when an approach turned out wrong.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _fn(
        "finish",
        "Call when the task is complete (tests pass) or you cannot make further progress.",
        {"summary": {"type": "string", "description": "What you changed and why."}},
        ["summary"],
    ),
]


REPORT_SCHEMA = _fn(
    "report",
    "Submit your final structured result and end the run. Only report what your commands showed.",
    {
        "verdict": {"type": "string", "enum": ["pass", "fail", "findings"]},
        "summary": {"type": "string", "description": "2-4 sentences for a busy developer."},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "detail": {"type": "string"},
                    "evidence_cmd": {"type": "string", "description": "Command whose output proves it."},
                },
                "required": ["title", "severity", "detail"],
            },
        },
    },
    ["verdict", "summary"],
)
MAX_SUMMARY = 1200
MAX_FINDINGS = 25


def validate_report(args: dict[str, Any]) -> dict[str, Any]:
    """Normalize a report; raises ValueError on malformed input. Output is treated as untrusted data."""
    verdict = args.get("verdict")
    if verdict not in ("pass", "fail", "findings"):
        raise ValueError("verdict must be pass, fail, or findings")
    summary = str(args.get("summary", "")).strip()[:MAX_SUMMARY]
    if not summary:
        raise ValueError("summary is required")
    findings = []
    for f in list(args.get("findings") or [])[:MAX_FINDINGS]:
        if not isinstance(f, dict) or not f.get("title"):
            raise ValueError("each finding needs a title")
        sev = f.get("severity", "info")
        findings.append({
            "title": str(f["title"])[:200],
            "severity": sev if sev in ("info", "low", "medium", "high", "critical") else "info",
            "file": str(f.get("file", ""))[:300],
            "line": f.get("line") if isinstance(f.get("line"), int) else None,
            "detail": str(f.get("detail", ""))[:1500],
            "evidence_cmd": str(f.get("evidence_cmd", ""))[:500],
        })
    return {"verdict": verdict, "summary": summary, "findings": findings}


@dataclass
class HostTool:
    """A tool that runs in the orchestrator (never in the sandbox), e.g. keyed web search."""

    schema: dict[str, Any]
    fn: Callable[..., Awaitable[str]]


def schemas_for(names: list[str] | None, host_tools: dict[str, HostTool] | None = None) -> list[dict[str, Any]]:
    """Tool schemas for an agent: a subset of built-ins, plus report, plus host tools."""
    allowed = [t for t in TOOL_SCHEMAS if names is None or t["function"]["name"] in names]
    if names is not None and "report" in names:
        allowed.append(REPORT_SCHEMA)
    return allowed + [h.schema for h in (host_tools or {}).values()]


@dataclass
class Workspace:
    """Tracks the agent's current snapshot plus named checkpoints."""

    sandbox: Sandbox
    snapshot: Snapshot
    test_command: str | None = None
    test_timeout: float = 600.0
    checkpoints: dict[str, Snapshot] = field(default_factory=dict)
    host_tools: dict[str, HostTool] = field(default_factory=dict)
    use_guard: bool = True
    audit: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.checkpoints.setdefault("start", self.snapshot)

    async def run_tests(self) -> tuple[bool, str]:
        if not self.test_command:
            return True, "No test command configured."
        res, _ = await self.sandbox.exec(
            self.snapshot, self.test_command, timeout=self.test_timeout, persist=False
        )
        out = f"$ {self.test_command}\nexit code: {res.exit_code}\n{res.stdout}\n{res.stderr}".strip()
        return res.ok, out

    async def call(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            return f"error: could not parse arguments as JSON ({e})"
        if name in self.host_tools:
            try:
                return clip(await self.host_tools[name].fn(**args))
            except TypeError as e:
                return f"error: bad arguments for {name}: {e}"
            except Exception as e:  # network errors etc. are reported to the model, not raised
                return f"error: {name} failed: {type(e).__name__}: {e}"
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"error: unknown tool {name!r}"
        try:
            return await handler(**args)
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"
        except (SandboxError, FileNotFoundError, UnicodeDecodeError) as e:
            return f"error: {e}"

    async def _tool_bash(self, command: str, timeout: float = 120) -> str:
        if self.use_guard:
            verdict = guard.check_command(command)
            self.audit.append({"command": command, "allowed": verdict.allowed, "rule": verdict.rule})
            if not verdict.allowed:
                return f"denied by guard ({verdict.rule}): {verdict.reason}. Choose a different approach."
        res, self.snapshot = await self.sandbox.exec(self.snapshot, command, timeout=float(timeout))
        self.evidence.append({"cmd": command, "exit": res.exit_code, "output_tail": (res.stdout + res.stderr)[-800:]})
        body = "\n".join(s for s in (res.stdout, res.stderr and f"[stderr]\n{res.stderr}") if s)
        return clip(f"exit code: {res.exit_code}\n{body}".rstrip())

    async def _tool_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        lines = (await self.sandbox.read(self.snapshot, path)).decode().splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or len(lines))
        numbered = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
        return clip(numbered or "(empty file)")

    async def _tool_write_file(self, path: str, content: str) -> str:
        self.snapshot = await self.sandbox.write(self.snapshot, {path: content.encode()})
        return f"wrote {path} ({len(content)} chars)"

    async def _tool_edit_file(self, path: str, old_text: str, new_text: str) -> str:
        text = (await self.sandbox.read(self.snapshot, path)).decode()
        count = text.count(old_text)
        if count != 1:
            return f"error: old_text found {count} times in {path}; it must match exactly once"
        self.snapshot = await self.sandbox.write(self.snapshot, {path: text.replace(old_text, new_text).encode()})
        return f"edited {path}"

    async def _tool_run_tests(self) -> str:
        _, out = await self.run_tests()
        return clip(out)

    async def _tool_checkpoint(self, name: str) -> str:
        self.checkpoints[name] = self.snapshot
        return f"checkpoint {name!r} saved (snapshot {self.snapshot.id})"

    async def _tool_rollback(self, name: str) -> str:
        if name not in self.checkpoints:
            return f"error: no checkpoint {name!r}; known: {', '.join(self.checkpoints)}"
        self.snapshot = self.checkpoints[name]
        return f"workspace restored to {name!r} (snapshot {self.snapshot.id})"

    async def _tool_finish(self, summary: str) -> str:
        return "finished"

    async def _tool_report(self, **args: Any) -> str:
        try:
            self.report = validate_report(args)
        except ValueError as e:
            return f"error: invalid report ({e}); fix it and call report again"
        return "report accepted"
