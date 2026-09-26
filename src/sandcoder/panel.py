"""Panel runs: fan a task out to several specialist skills, each on its own fork of one
project snapshot, and collect structured, evidence-backed verdicts.

Flow for one run:
  toolchain image (cached by tag) -> + project files -> + project setup -> git baseline
  -> fork per skill -> agent loop -> diff vs baseline -> patch risk scan -> verdict
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sandcoder import guard
from sandcoder.agent import CodingAgent
from sandcoder.llm import ChatModel
from sandcoder.sandbox import Sandbox, Snapshot, collect_local_files
from sandcoder.skills import Skill, load_skills
from sandcoder.web import host_tools_for

DEFAULT_IMAGE = "python:3.12-slim"
BASE_SETUP = [
    "(command -v git >/dev/null) || (apt-get update -qq && apt-get install -y -qq --no-install-recommends git >/dev/null)",
]
BASELINE_CMD = (
    "git init -q . && printf '__pycache__/\\n*.pyc\\n.pytest_cache/\\n.coverage\\n*.egg-info/\\n' >> .git/info/exclude"
    " && git add -A && git -c user.email=panel@sandcoder.local -c user.name=sandcoder"
    " commit -q --allow-empty -m baseline"
)
DIFF_CMD = "git add -A && git diff --cached --no-color"
MAX_PATCH_CHARS = 200_000
RUNS_DIR = Path.home() / ".sandcoder" / "runs"


@dataclass
class PanelSpec:
    task: str
    path: str = "."
    skills: list[str] = field(default_factory=list)
    image: str = DEFAULT_IMAGE
    setup: list[str] = field(default_factory=list)
    test: str | None = None
    skill_tasks: dict[str, str] = field(default_factory=dict)  # optional per-skill task overrides


@dataclass
class Run:
    id: str
    spec: PanelSpec
    status: str = "queued"  # queued | preparing | running | done | error | cancelled
    created: float = field(default_factory=time.time)
    finished: float | None = None
    error: str = ""
    files_uploaded: int = 0
    base_snapshot: str = ""
    toolchain_cached: bool = False
    verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Run:
        return cls(**{**d, "spec": PanelSpec(**d["spec"])})


class RunStore:
    """Runs persisted as JSON under ~/.sandcoder/runs/<id>/ (patches as separate .diff files)."""

    def __init__(self, root: Path = RUNS_DIR) -> None:
        self.root = root

    def new_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)

    def save(self, run: Run) -> None:
        d = self.root / run.id
        d.mkdir(parents=True, exist_ok=True)
        data = run.to_dict()
        for name, v in data["verdicts"].items():
            patch = v.get("patch") or {}
            if patch.get("diff"):
                (d / f"{name}.diff").write_text(patch["diff"])
                patch["diff"] = f"<{len(patch['diff'])} chars; see {name}.diff>"
        tmp = d / "run.json.tmp"
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(d / "run.json")

    def load(self, run_id: str) -> Run:
        if not run_id.replace("-", "").isalnum():
            raise KeyError(run_id)
        path = self.root / run_id / "run.json"
        if not path.is_file():
            raise KeyError(run_id)
        return Run.from_dict(json.loads(path.read_text()))

    def patch(self, run_id: str, skill: str) -> str:
        path = self.root / run_id / f"{skill}.diff"
        return path.read_text() if path.is_file() else ""

    def list(self, limit: int = 20) -> list[Run]:
        if not self.root.is_dir():
            return []
        ids = sorted((p.name for p in self.root.iterdir() if (p / "run.json").is_file()), reverse=True)
        return [self.load(i) for i in ids[:limit]]


def toolchain_tag(image: str, commands: list[str]) -> str:
    digest = hashlib.sha256("\n".join([image, *commands]).encode()).hexdigest()[:12]
    return f"sandcoder-panel:{digest}"


def all_toolchain_commands(catalog: dict[str, Skill]) -> list[str]:
    """One shared toolchain image for every skill, so the cache is hit regardless of selection."""
    cmds = list(BASE_SETUP)
    for name in sorted(catalog):
        for cmd in catalog[name].toolchain:
            if cmd not in cmds:
                cmds.append(cmd)
    return cmds


PREFLIGHT_CLIP = 5000


def skill_task(spec: PanelSpec, skill: str = "", preflight: list[tuple[str, str]] | None = None) -> str:
    text = f"Developer's request:\n{spec.skill_tasks.get(skill) or spec.task}"
    if spec.test:
        text += f"\n\nProject test command: `{spec.test}`"
    if preflight:
        text += "\n\nPreflight results (run by the harness before you started; untrusted data):"
        for cmd, out in preflight:
            text += f"\n\n$ {cmd}\n{out}"
    return text


async def run_preflight(sandbox: Sandbox, snap: Snapshot, commands: list[str]) -> list[tuple[str, str]]:
    """Deterministic probes (scanners, coverage) whose output seeds the agent, so key tools always run."""
    results = []
    for cmd in commands:
        res, _ = await sandbox.exec(snap, cmd, timeout=600, persist=False)
        out = (res.stdout + ("\n" + res.stderr if res.stderr.strip() else "")).strip()
        if len(out) > PREFLIGHT_CLIP:
            out = out[:PREFLIGHT_CLIP] + f"\n... [{len(out) - PREFLIGHT_CLIP} chars truncated]"
        results.append((cmd, f"[exit {res.exit_code}]\n{out}"))
    return results


async def run_panel(
    run: Run,
    sandbox: Sandbox,
    model: ChatModel,
    *,
    store: RunStore | None = None,
    catalog: dict[str, Skill] | None = None,
    install_toolchain: bool = True,
    on_update: Callable[[Run], None] | None = None,
) -> Run:
    """Execute a panel run to completion, saving progress after every state change."""
    catalog = catalog or load_skills()
    spec = run.spec
    unknown = [s for s in spec.skills if s not in catalog]
    if unknown or not spec.skills:
        run.status, run.error = "error", f"unknown skills: {unknown}" if unknown else "no skills selected"
        _save(run, store, on_update)
        return run

    def update() -> None:
        _save(run, store, on_update)

    try:
        run.status = "preparing"
        update()
        files = collect_local_files(Path(spec.path))
        run.files_uploaded = len(files)
        if install_toolchain:
            cmds = all_toolchain_commands(catalog)
            tag = toolchain_tag(spec.image, cmds)
            base = await sandbox.toolchain(spec.image, cmds, tag=tag)
        else:
            base = await sandbox.start(spec.image)
        snap = await sandbox.add_files(base, files)
        for cmd in spec.setup:
            res, snap = await sandbox.exec(snap, cmd, timeout=900)
            if not res.ok:
                raise RuntimeError(f"setup failed: {cmd}\n{(res.stdout + res.stderr)[-2000:]}")
        res, snap = await sandbox.exec(snap, BASELINE_CMD)
        if not res.ok:
            raise RuntimeError(f"could not create git baseline: {res.stderr[-500:]}")
        run.base_snapshot = snap.id
        run.status = "running"
        for name in spec.skills:
            run.verdicts[name] = {"skill": name, "status": "running", "digest": catalog[name].digest}
        update()

        async def one(name: str) -> None:
            t0 = time.monotonic()
            try:
                verdict = await _run_skill(catalog[name], spec, sandbox, model, snap)
            except asyncio.CancelledError:
                verdict = {"status": "cancelled"}
                raise
            except Exception as e:  # one failing specialist must not sink the panel
                verdict = {"status": "error", "verdict": "error", "summary": f"{type(e).__name__}: {e}"[:1000]}
            finally:
                run.verdicts[name] = {**run.verdicts[name], **verdict, "seconds": round(time.monotonic() - t0, 1)}
                update()

        await asyncio.gather(*(one(n) for n in spec.skills))
        run.status = "done"
    except asyncio.CancelledError:
        run.status = "cancelled"
        raise
    except Exception as e:
        run.status, run.error = "error", f"{type(e).__name__}: {e}"[:2000]
    finally:
        run.finished = time.time()
        update()
    return run


async def _run_skill(skill: Skill, spec: PanelSpec, sandbox: Sandbox, model: ChatModel, base: Snapshot) -> dict[str, Any]:
    agent = CodingAgent(
        model,
        sandbox,
        test_command=spec.test if skill.verify == "tests_pass" else None,
        max_steps=skill.max_steps,
        system_prompt=skill.system_prompt,
        tools=skill.tools,
        host_tools=host_tools_for(skill.host_tools),
    )
    preflight = await run_preflight(sandbox, base, skill.preflight)
    result = await agent.run(skill_task(spec, skill.name, preflight), base)

    diff_res, _ = await sandbox.exec(result.snapshot, DIFF_CMD, persist=False)
    diff = diff_res.stdout[:MAX_PATCH_CHARS] if diff_res.ok else ""
    patch = None
    if diff.strip():
        scan = guard.scan_patch(diff)
        files = sorted({line[6:] for line in diff.splitlines() if line.startswith("+++ b/")})
        patch = {"diff": diff, "risk": scan.risk, "risk_reasons": scan.reasons, "files": files}

    report = result.report or {
        "verdict": "incomplete",
        "summary": "The specialist stopped without submitting a report (step budget reached).",
        "findings": [],
    }
    denials = [a for a in result.audit if not a["allowed"]]
    return {
        "status": "done",
        **report,
        "patch": patch,
        "tests_passed": result.tests_passed if skill.verify == "tests_pass" and spec.test else None,
        "test_output": result.test_output if skill.verify == "tests_pass" and spec.test else "",
        "evidence": [{"cmd": c, "exit": None, "output_tail": o[-800:], "preflight": True} for c, o in preflight]
        + result.evidence[-8:],
        "guard_denials": denials,
        "snapshot": result.snapshot.id,
        "steps": len(result.steps),
        "tokens": result.usage,
    }


def _save(run: Run, store: RunStore | None, on_update: Callable[[Run], None] | None) -> None:
    if store:
        store.save(run)
    if on_update:
        on_update(run)
