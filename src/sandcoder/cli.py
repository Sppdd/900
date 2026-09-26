"""Command line: `sandcoder run | panel | skills | exec | images | models`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sandcoder.agent import AgentResult, CodingAgent, Step
from sandcoder.llm import OpenAIChat
from sandcoder.sandbox import ContreeSandbox, LocalSandbox, Sandbox, Snapshot, collect_local_files
from sandcoder.search import best_of_n


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def make_sandbox(args: argparse.Namespace) -> Sandbox:
    if args.local:
        log("WARNING: --local runs model-written code directly on this machine with no isolation.")
        return LocalSandbox()
    return ContreeSandbox.from_env()


async def prepare(sb: Sandbox, args: argparse.Namespace) -> Snapshot:
    files = collect_local_files(Path(args.workspace)) if args.workspace else {}
    log(f"• starting sandbox from {args.image} with {len(files)} file(s)")
    snap = await sb.start(args.image, files)
    for cmd in args.setup or []:
        log(f"• setup: {cmd}")
        res, snap = await sb.exec(snap, cmd, timeout=args.setup_timeout)
        if not res.ok:
            raise SystemExit(f"setup failed (exit {res.exit_code}):\n{res.stdout}\n{res.stderr}")
    return snap


def step_logger(prefix: str):
    def on_step(step: Step) -> None:
        first = step.output.splitlines()[0] if step.output else ""
        args = step.arguments if len(step.arguments) < 100 else step.arguments[:97] + "..."
        log(f"  {prefix}{step.tool}({args}) → {first[:100]}")

    return on_step


async def cmd_run(args: argparse.Namespace) -> int:
    task = Path(args.task[1:]).read_text() if args.task.startswith("@") else args.task
    model = OpenAIChat(args.model, temperature=args.temperature)
    log(f"• model: {model.model}")

    async with make_sandbox(args) as sb:
        base = await prepare(sb, args)

        def make_agent(i: int) -> CodingAgent:
            # Spread temperatures so parallel attempts explore different solutions.
            m = model if args.attempts == 1 else model.with_temperature(min(1.0, args.temperature + 0.25 * i))
            prefix = f"[{i}] " if args.attempts > 1 else ""
            return CodingAgent(
                m, sb, test_command=args.test, max_steps=args.max_steps, on_step=step_logger(prefix)
            )

        if args.attempts == 1:
            best: AgentResult = await make_agent(0).run(task, base)
            all_results: list = [best]
        else:
            log(f"• forking {args.attempts} attempts from snapshot {base.id}")
            best, all_results = await best_of_n(
                make_agent,
                task,
                base,
                args.attempts,
                on_done=lambda i, r: log(f"• attempt {i} done: {r.status} in {len(r.steps)} steps"),
            )

        log(f"\n• result: {best.status} ({len(best.steps)} steps, snapshot {best.snapshot.id})")
        if best.summary:
            log(f"• summary: {best.summary}")
        if args.test:
            log(f"• verification:\n{best.test_output}")

        if args.out:
            dest = await sb.export(best.snapshot, Path(args.out))
            log(f"• workspace exported to {dest}")
        if args.trace:
            trace = [r.to_dict() if isinstance(r, AgentResult) else {"error": repr(r)} for r in all_results]
            Path(args.trace).write_text(json.dumps(trace, indent=2))
            log(f"• trace written to {args.trace}")

    return 0 if best.tests_passed else 1


async def cmd_exec(args: argparse.Namespace) -> int:
    async with make_sandbox(args) as sb:
        snap = await prepare(sb, args)
        res, _ = await sb.exec(snap, " ".join(args.command), timeout=args.timeout, persist=False)
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        return res.exit_code


async def cmd_images(args: argparse.Namespace) -> int:
    async with ContreeSandbox.from_env() as sb:
        for uid, tag in await sb.list_images(args.limit):  # type: ignore[attr-defined]
            print(f"{uid}  {tag or ''}")
    return 0


async def cmd_panel(args: argparse.Namespace) -> int:
    from sandcoder.panel import PanelSpec, Run, RunStore, run_panel
    from sandcoder.mcp_server import summarize

    task = Path(args.task[1:]).read_text() if args.task.startswith("@") else args.task
    store = RunStore()
    spec = PanelSpec(
        task=task, path=str(Path(args.workspace).resolve()), skills=args.skills.split(","),
        image=args.image, setup=args.setup or [], test=args.test,
        skill_tasks={k: (Path(v[1:]).read_text() if v.startswith("@") else v)
                     for k, v in (item.split("=", 1) for item in args.skill_task or [])},
    )
    run = Run(id=store.new_id(), spec=spec)
    seen: dict[str, str] = {}

    def on_update(r: Run) -> None:
        if seen.get("_run") != r.status:
            seen["_run"] = r.status
            log(f"• run {r.id}: {r.status}" + (f" ({r.files_uploaded} files uploaded)" if r.status == "running" else ""))
        for name, v in r.verdicts.items():
            if v.get("status") != seen.get(name):
                seen[name] = v.get("status", "")
                if v.get("status") != "running":
                    log(f"  [{name}] {v.get('status')}: {v.get('verdict')} in {v.get('seconds')}s")

    async with make_sandbox(args) as sb:
        run = await run_panel(
            run, sb, OpenAIChat(args.model), store=store, install_toolchain=not args.local, on_update=on_update
        )
    print(json.dumps(summarize(run, store), indent=2))
    log(f"• full record: {store.root / run.id}")
    return 0 if run.status == "done" else 1


async def cmd_models(args: argparse.Namespace) -> int:
    for mid in await OpenAIChat().list_models():
        if args.all or "nemotron" in mid.lower() or mid.lower().startswith("nvidia/"):
            print(mid)
    return 0


async def cmd_skills(args: argparse.Namespace) -> int:
    from sandcoder.skills import load_skills

    for s in load_skills().values():
        print(f"{s.name:20} {s.digest}  {s.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sandcoder", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def sandbox_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--image", default="python:3.12-slim", help="Sandbox image tag, UUID, or docker:// ref")
        sp.add_argument("-w", "--workspace", help="Local directory uploaded as the project")
        sp.add_argument("--setup", action="append", help="Command run once before the agent (repeatable)")
        sp.add_argument("--setup-timeout", type=float, default=900)
        sp.add_argument("--local", action="store_true", help="Use the UNISOLATED local backend (testing only)")

    r = sub.add_parser("run", help="Run a coding agent on a task")
    r.add_argument("task", help="Task text, or @path/to/task.md")
    sandbox_opts(r)
    r.add_argument("-t", "--test", help="Command that verifies the task, e.g. 'pytest -q'")
    r.add_argument("-n", "--attempts", type=int, default=1, help="Parallel attempts forked from one snapshot")
    r.add_argument("--max-steps", type=int, default=40)
    r.add_argument("--model", help="Model id (default: $SANDCODER_MODEL or nvidia/nemotron-3-super-120b-a12b)")
    r.add_argument("--temperature", type=float, default=0.2)
    r.add_argument("-o", "--out", help="Export the winning workspace to this local directory")
    r.add_argument("--trace", help="Write a JSON trajectory of every attempt")
    r.set_defaults(fn=cmd_run)

    e = sub.add_parser("exec", help="Upload a workspace and run one command in a sandbox")
    sandbox_opts(e)
    e.add_argument("--timeout", type=float, default=300)
    e.add_argument("command", nargs=argparse.REMAINDER)
    e.set_defaults(fn=cmd_exec)

    i = sub.add_parser("images", help="List images available to your project")
    i.add_argument("--limit", type=int, default=50)
    i.set_defaults(fn=cmd_images)

    pn = sub.add_parser("panel", help="Run specialist skills in parallel sandboxes and print their verdicts")
    pn.add_argument("task", help="What to check/do, or @file")
    sandbox_opts(pn)
    pn.set_defaults(workspace=".")
    pn.add_argument("-s", "--skills", default="security-auditor,test-writer", help="Comma-separated skill names")
    pn.add_argument("-t", "--test", help="Project test command")
    pn.add_argument("--skill-task", action="append", help="Per-skill task: NAME=text or NAME=@file (repeatable)")
    pn.add_argument("--model", help="Model id (default: Nemotron 3 Super)")
    pn.set_defaults(fn=cmd_panel)

    sk = sub.add_parser("skills", help="List specialist skills")
    sk.set_defaults(fn=cmd_skills)

    m = sub.add_parser("models", help="List Token Factory models (NVIDIA/Nemotron by default)")
    m.add_argument("--all", action="store_true", help="Show every model, not just NVIDIA ones")
    m.set_defaults(fn=cmd_models)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        sys.exit(asyncio.run(args.fn(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
