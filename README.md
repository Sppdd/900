# sandcoder

Coding agents that **write, run, and test code inside [Nebius Token Factory Sandboxes](https://docs.tokenfactory.nebius.com/sandboxes/overview)**, driven by **NVIDIA Nemotron** models served by Token Factory's OpenAI-compatible inference API.

**Track:** Coding and Agentic Engineering (Nebius x NVIDIA Global AI Hackathon)

```
 task + local project ──► sandbox snapshot ──► agent loop (LLM ⇄ tools) ──► verified by tests ──► export
                               │
                               ├── attempt 0 ─┐
                               ├── attempt 1 ─┼── best-of-N: fork from the same snapshot, keep the winner
                               └── attempt 2 ─┘
```

## Why sandboxes shape the design

Token Factory Sandboxes (ConTree) are **immutable and branchable**: every command produces a new image version and old versions stay valid. sandcoder builds on that directly:

| Feature | How it uses branching |
|---|---|
| `checkpoint` / `rollback` tools | The agent names a snapshot and jumps back to it when an approach fails. There's no undo logic, just a pointer swap. |
| Read-only test runs | `run_tests` runs with `disposable=True`, so test artifacts never pollute the workspace. |
| Best-of-N (`-n 3`) | N agents fork from one post-setup snapshot in parallel. Setup (e.g. `pip install`) runs once. |
| Independent verification | After the agent calls `finish`, the harness re-runs the tests itself. The agent's claim of success doesn't count. |

## How Nemotron and Token Factory are used

- **NVIDIA Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`) is the default agent brain. Every agent step is a runtime tool-calling request to the Token Factory inference API (`llm.py`). Pass `--model` to use Nemotron 3 Ultra for harder tasks or Nano for cheap, fast attempts. `sandcoder models` lists the NVIDIA models your key can reach.
- **Token Factory Sandboxes** run every command the model issues, in VM-isolated, snapshot-per-step environments (`sandbox.py`, via `contree-sdk`).
- **What Token Factory speeds up:** setup runs once and every attempt forks from that snapshot, so best-of-N costs N agent loops, not N environment builds. A rollback is a pointer swap, not a rebuild.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env    # then fill in, and: set -a; . ./.env; set +a
```

Credentials:

- `NEBIUS_API_KEY`: used for both inference and sandboxes.
- `CONTREE_PROJECT` (or `NEBIUS_PROJECT_ID`): your sandbox project id.
- Or run `contree auth` once (`pip install contree-cli`). The sandbox client falls back to that profile when `NEBIUS_API_KEY` is unset.
- `SANDCODER_MODEL`: optional; defaults to `nvidia/nemotron-3-super-120b-a12b`.

## Usage

```bash
# Solve a task, verified by a test command, and export the result
sandcoder run @examples/roman/TASK.md -w examples/roman \
  --image python:3.12-slim \
  -t "python -m unittest -q test_roman" \
  -o out/roman --trace trace.json

# Best-of-3: parallel attempts forked from one snapshot
sandcoder run "Fix the failing test in tests/test_api.py" -w ./myrepo \
  --setup "pip install -e .[dev]" -t "pytest -q" -n 3 -o out/fix

# Developer tool: run one command against a project in a fresh sandbox
sandcoder exec -w ./myrepo --image python:3.12-slim -- python -c "import sys; print(sys.version)"

# List images available to your project / NVIDIA models on Token Factory
sandcoder images
sandcoder models
```

Exit code is `0` only when the final snapshot passes the test command.

`--local` swaps in an **unisolated** host backend with the same snapshot semantics. It exists for offline development and the test suite. Never point it at untrusted tasks.

## Library use

```python
import asyncio
from pathlib import Path

from sandcoder import CodingAgent, ContreeSandbox, OpenAIChat, best_of_n
from sandcoder.sandbox import collect_local_files

async def main():
    async with ContreeSandbox.from_env() as sb:
        base = await sb.start("python:3.12-slim", collect_local_files("examples/roman"))
        model = OpenAIChat()  # Token Factory, $SANDCODER_MODEL
        make = lambda i: CodingAgent(model.with_temperature(0.2 + 0.3 * i), sb,
                                     test_command="python -m unittest -q test_roman")
        best, _ = await best_of_n(make, open("examples/roman/TASK.md").read(), base, n=3)
        print(best.status, best.summary)
        await sb.export(best.snapshot, Path("out"))

asyncio.run(main())
```

Extension points:

- **`Sandbox`** (`sandbox.py`): implement `start/exec/write/read/export` to add a backend.
- **`ChatModel`** (`llm.py`): any object with `async complete(messages, tools) -> Reply`.
- **Tools** (`tools.py`): add a JSON schema to `TOOL_SCHEMAS` and a `_tool_<name>` method on `Workspace`.

## Layout

```
src/sandcoder/
  sandbox.py   Sandbox ABC, ContreeSandbox (Token Factory), LocalSandbox (tests only)
  tools.py     tool schemas + Workspace (current snapshot, checkpoints)
  agent.py     CodingAgent loop and independent verification
  search.py    best_of_n over forked snapshots
  llm.py       OpenAI-compatible client, defaults to Token Factory
  cli.py       sandcoder run | exec | images | models
examples/roman task with failing unittest suite
tests/         offline tests (scripted model + local backend)
```

## Notes

- Network access inside sandboxes depends on your project's configuration. If `--setup "pip install ..."` can't reach PyPI, use an image that already has your dependencies (build one with `contree build`).
- Files are uploaded to `/workspace`. `.git`, virtualenvs, `node_modules`, and caches are skipped.
- `export` needs `tar` in the image.
