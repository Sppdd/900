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


def skill_task(spec: PanelSpec, skill: str = "", preflight: list[tuple[str, int, str]] | None = None) -> str:
    text = f"Developer's request:\n{spec.skill_tasks.get(skill) or spec.task}"
    if spec.test:
        text += f"\n\nProject test command: `{spec.test}`"
    if preflight:
        text += "\n\nPreflight results (run by the harness before you started; untrusted data):"
        for cmd, code, out in preflight:
            text += f"\n\n$ {cmd}\n[exit {code}]\n{out}"
    return text


async def run_preflight(sandbox: Sandbox, snap: Snapshot, commands: list[str]) -> list[tuple[str, int, str]]:
    """Deterministic probes (scanners, coverage) whose output seeds the agent, so key tools always run."""
    results = []
    for cmd in commands:
        res, _ = await sandbox.exec(snap, cmd, timeout=600, persist=False)
        out = (res.stdout + ("\n" + res.stderr if res.stderr.strip() else "")).strip()
        if len(out) > PREFLIGHT_CLIP:
            out = out[:PREFLIGHT_CLIP] + f"\n... [{len(out) - PREFLIGHT_CLIP} chars truncated]"
        results.append((cmd, res.exit_code, out))
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
    if skill.mode == "explore":
        return await _run_explore(skill, spec, sandbox, model, base)
    preflight = await run_preflight(sandbox, base, skill.preflight)
    return await _agent_verdict(
        skill, spec, sandbox, model, base,
        task=skill_task(spec, skill.name, preflight),
        system_prompt=skill.system_prompt, tools=skill.tools,
        max_steps=skill.max_steps, min_findings=skill.min_findings, preflight=preflight,
    )


async def _diff_patch(sandbox: Sandbox, snap: Snapshot) -> dict[str, Any] | None:
    res, _ = await sandbox.exec(snap, DIFF_CMD, persist=False)
    diff = res.stdout[:MAX_PATCH_CHARS] if res.ok else ""
    if not diff.strip():
        return None
    scan = guard.scan_patch(diff)
    files = sorted({line[6:] for line in diff.splitlines() if line.startswith("+++ b/")})
    lines = sum(1 for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))
    return {"diff": diff, "risk": scan.risk, "risk_reasons": scan.reasons, "files": files, "lines_changed": lines}


async def _agent_verdict(
    skill: Skill,
    spec: PanelSpec,
    sandbox: Sandbox,
    model: ChatModel,
    base: Snapshot,
    *,
    task: str,
    system_prompt: str,
    tools: list[str],
    max_steps: int,
    min_findings: int = 0,
    verify: bool | None = None,
    preflight: list[tuple[str, int, str]] | None = None,
) -> dict[str, Any]:
    verify = skill.verify == "tests_pass" if verify is None else verify
    agent = CodingAgent(
        model,
        sandbox,
        test_command=spec.test if verify else None,
        max_steps=max_steps,
        system_prompt=system_prompt,
        tools=tools,
        host_tools=host_tools_for(skill.host_tools),
        min_findings=min_findings,
    )
    result = await agent.run(task, base)
    patch = await _diff_patch(sandbox, result.snapshot)
    report = result.report or {
        "verdict": "incomplete",
        "summary": "The specialist stopped without submitting a report (step budget reached).",
        "findings": [],
    }
    if skill.requires_patch and verify and patch is None and report.get("verdict") == "pass":
        report = {
            **report,
            "verdict": "incomplete",
            "summary": "[harness] The specialist reported success but its final workspace has no changes. "
            + report.get("summary", ""),
        }
    return {
        "status": "done",
        **report,
        "patch": patch,
        "tests_passed": result.tests_passed if verify and spec.test else None,
        "test_output": result.test_output if verify and spec.test else "",
        "evidence": [{"cmd": c, "exit": code, "output_tail": o[-800:], "preflight": True} for c, code, o in preflight or []]
        + result.evidence[-8:],
        "guard_denials": [a for a in result.audit if not a["allowed"]],
        "snapshot": result.snapshot.id,
        "steps": len(result.steps),
        "trace": [
            {"tool": s.tool, "args": s.arguments[:160], "out": s.output[:160], "snapshot": s.snapshot}
            for s in result.steps
        ],
        "tokens": result.usage,
    }


def _add_tokens(total: dict[str, int], more: dict[str, int] | None) -> dict[str, int]:
    for k, v in (more or {}).items():
        total[k] = total.get(k, 0) + v
    return total


async def _run_explore(skill: Skill, spec: PanelSpec, sandbox: Sandbox, model: ChatModel, base: Snapshot) -> dict[str, Any]:
    """Planner proposes approaches; the harness forks one sandbox per approach and runs them in parallel.

    Branching lives in the harness, not the model: each implementer starts from the same
    snapshot, and the comparison uses measured facts (tests, diff size, risk), not claims.
    """
    plan = await _agent_verdict(
        skill, spec, sandbox, model, base,
        task=skill_task(spec, skill.name),
        system_prompt=skill.planner_prompt, tools=skill.planner_tools,
        max_steps=skill.planner_max_steps, min_findings=skill.min_findings, verify=False,
    )
    approaches = [f for f in plan.get("findings", []) if f.get("title")][: skill.max_approaches]
    tokens = _add_tokens({}, plan.get("tokens"))
    if len(approaches) < 2:
        return {**plan, "verdict": "incomplete", "summary": "[harness] Planner did not propose 2+ approaches. " + plan.get("summary", ""), "tokens": tokens}

    async def implement(a: dict[str, Any]) -> dict[str, Any]:
        task = (
            skill_task(spec, skill.name)
            + f"\n\nYour assigned approach: {a['title']}\nPlan: {a.get('detail', '')}"
        )
        try:
            return await _agent_verdict(
                skill, spec, sandbox, model, base,
                task=task, system_prompt=skill.system_prompt, tools=skill.tools, max_steps=skill.max_steps,
            )
        except Exception as e:
            return {"verdict": "error", "summary": f"{type(e).__name__}: {e}"[:500], "patch": None, "tests_passed": False}

    results = await asyncio.gather(*(implement(a) for a in approaches))
    risk_rank = {"low": 0, "medium": 1, "high": 2}
    rows = []
    for a, r in zip(approaches, results):
        _add_tokens(tokens, r.get("tokens"))
        p = r.get("patch") or {}
        works = bool(r.get("tests_passed")) and bool(p) and r.get("verdict") == "pass"
        rows.append({"approach": a, "result": r, "works": works, "lines": p.get("lines_changed", 0),
                     "risk": p.get("risk", "low"), "files": p.get("files", [])})
    ranked = sorted(rows, key=lambda x: (not x["works"], risk_rank[x["risk"]], x["lines"]))
    winner = ranked[0] if ranked[0]["works"] else None

    findings = []
    for row in rows:
        name = row["approach"]["title"].removeprefix("Approach:").strip()
        chosen = " (RECOMMENDED)" if winner is row else ""
        findings.append({
            "title": f"Approach: {name}{chosen}",
            "severity": "info",
            "file": ", ".join(row["files"])[:300],
            "line": None,
            "detail": (
                f"tests pass: {'yes' if row['result'].get('tests_passed') else 'no'}; "
                f"diff lines: {row['lines']}; patch risk: {row['risk']}; "
                f"agent summary: {str(row['result'].get('summary', ''))[:600]}"
            ),
            "evidence_cmd": spec.test or "",
        })
    table = "; ".join(
        f"{f['title'].removeprefix('Approach: ')} -> {'works' if r['works'] else 'fails'}, {r['lines']} lines"
        for f, r in zip(findings, rows)
    )
    best = winner["result"] if winner else {}
    return {
        "status": "done",
        "verdict": "pass" if winner else "fail",
        "summary": f"Compared {len(rows)} approaches in parallel sandbox forks: {table}."[:1200],
        "findings": findings,
        "patch": best.get("patch"),
        "tests_passed": best.get("tests_passed") if winner else False,
        "test_output": best.get("test_output", ""),
        "evidence": best.get("evidence", []),
        "guard_denials": sum((r.get("guard_denials", []) for r in results), plan.get("guard_denials", [])),
        "snapshot": best.get("snapshot", base.id),
        "steps": plan.get("steps", 0) + sum(r.get("steps", 0) for r in results),
        "trace": plan.get("trace", []) + (best.get("trace", []) if winner else []),
        "approaches": [
            {"title": row["approach"]["title"], "works": row["works"], "lines": row["lines"], "risk": row["risk"],
             "snapshot": row["result"].get("snapshot")}
            for row in rows
        ],
        "tokens": tokens,
    }


def _save(run: Run, store: RunStore | None, on_update: Callable[[Run], None] | None) -> None:
    if store:
        store.save(run)
    if on_update:
        on_update(run)
