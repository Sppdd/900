# sandcoder

**A panel of sandboxed specialist agents for your coding agent.** Claude Code, Codex or Cursor calls one MCP tool, and a security auditor, test writer, bug reproducer and feature researcher each fork the same snapshot of your repo in an isolated **Nebius Token Factory Sandbox**. They run real tools in parallel, driven by **NVIDIA Nemotron**, and return evidence: proof-of-concept tests, passing test patches, failing regression tests, and risk-rated diffs.

**Track:** Coding and Agentic Engineering (Nebius x NVIDIA Global AI Hackathon)

```
your coding agent ──MCP──▶ sandcoder-mcp (local; holds keys, uploads repo minus secrets)
                              │  Nemotron loops run here; sandboxes never see a key
                              ▼
                  cached toolchain snapshot → + repo + deps → git baseline
                     ├─ fork: security-auditor   bandit/semgrep/pip-audit → PoC tests
                     ├─ fork: test-writer        coverage → passing tests
                     ├─ fork: bug-reproducer     bug report → failing regression test
                     └─ fork: feature-researcher planner → one fork per approach → measured winner
                              ▼
                  verdicts + evidence + risk-rated diffs → your agent (never auto-applied)
```

## A real run

On `examples/buggy-api` (a small API with planted flaws), with Nemotron 3 Super on Token Factory Sandboxes, three specialists ran in parallel (longest took 117 s):

| Specialist | Result |
|---|---|
| security-auditor | **Medium: SQL injection** in `app/users.py:6`. It wrote a PoC test that failed on the original code and passed after its parameterized-query patch. |
| test-writer | **+5 tests**, coverage **67% → 100%**, all passing (re-verified by the harness) |
| bug-reproducer | **Failing regression test** for the off-by-one bulk discount (`>` vs `>=` at `pricing.py:10`) |

The feature-researcher, asked to add `GET /orders.csv`, proposed stdlib `csv` and pandas. It implemented both in parallel sandbox forks (both passed tests) and recommended stdlib `csv`: the harness rated the pandas patch medium-risk because it adds a dependency.

Every command a specialist ran is recorded as evidence, and each result links to a sandbox snapshot.

## Install

You need a Token Factory API key, a project id with Sandboxes enabled, and [`uv`](https://docs.astral.sh/uv/).

```bash
export NEBIUS_API_KEY=...        # Token Factory key (inference + sandboxes)
export NEBIUS_PROJECT_ID=...     # Token Factory project with Sandboxes enabled
export TAVILY_API_KEY=...        # optional: web search for feature-researcher
```

**Claude Code plugin** (MCP server + `sandbox-panel` skill + `/panel` command):
```
/plugin marketplace add Sppdd/900
/plugin install sandcoder@sandcoder
```

**Claude Code, MCP only:**
```bash
claude mcp add sandcoder -e NEBIUS_API_KEY="$NEBIUS_API_KEY" -e NEBIUS_PROJECT_ID="$NEBIUS_PROJECT_ID" \
  -- uvx --from git+https://github.com/Sppdd/900 sandcoder-mcp
```

**Codex** (`~/.codex/config.toml`) and **Cursor** (`.cursor/mcp.json`) use the same `uvx --from git+https://github.com/Sppdd/900 sandcoder-mcp` command. See the website (`site/`) for copy-paste snippets.

Then ask your agent: *"check this repo before I open the PR"*, or use the prompts `panel_premerge`, `panel_repro`, `panel_research`.

## MCP tools

| Tool | Purpose |
|---|---|
| `panel_run(task, skills?, path?, test?, setup?, image?, skill_tasks?)` | Start specialists; returns `run_id` immediately (async) |
| `panel_results(run_id, wait_seconds?)` | Verdicts, findings, test status, patch summaries, guard denials, tokens |
| `panel_patch(run_id, skill)` | A specialist's diff with risk rating and reasons |
| `panel_cancel(run_id)`, `panel_runs()`, `skills_list()` | Manage runs, list skills |

Runs are stored in `~/.sandcoder/runs/<run_id>/` (`run.json` plus one `.diff` per skill).

## How Nemotron and Token Factory are used

- **NVIDIA Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`) drives every specialist's tool-calling loop through the Token Factory inference API. Override it with `SANDCODER_MODEL`. `sandcoder models` lists the NVIDIA models your key can reach.
- **Token Factory Sandboxes** run every command:
  - **Tagged toolchain image**, built once, so later runs skip installing scanners.
  - **Immutable snapshots**, so each specialist forks the same prepared state and runs in parallel.
  - **Checkpoint and rollback** inside a skill, e.g. the researcher trying several approaches.
  - **Disposable runs** for tests and diffs.
- **What Token Factory speeds up:** setup runs once per panel, not once per specialist, and repeat runs reuse the cached toolchain image. The expensive host model only reads short verdicts.

## Safety

1. **Isolation:** VM-isolated, disposable sandboxes with no credentials. Model and web-search calls run in the MCP process.
2. **No secrets uploaded:** uploads respect `.gitignore` and always drop `.env*`, keys, `.npmrc`, `.pypirc`, tfstate and similar files (`sandbox.collect_local_files`).
3. **Command guard** (`guard.check_command`): blocks exfiltration, `curl | sh`, env dumps, `~/.ssh`, reverse shells, miners and unknown hosts. Every command is logged.
4. **Patch scanner** (`guard.scan_patch`): flags weakened tests, CI edits, dependency changes, network calls, `eval` and encoded blobs.
5. **Results are data:** reports are schema-validated and length-capped. The plugin skill forbids following instructions in results and auto-applying patches.

Layers 3–4 catch common attacks, not a determined adversary. Isolation and human review of diffs are the boundary.

## Bring your own skills

Any standard `SKILL.md` skill works. Examples: [mattpocock/skills](https://github.com/mattpocock/skills), your `~/.claude/skills`, ones [autoharness](https://github.com/tigerless-labs/autoharness) learned, or your team's.

```bash
sandcoder skills add github:mattpocock/skills/skills/engineering --only tdd,diagnosing-bugs   # reviewed, then pinned
sandcoder skills add ~/.claude/skills/my-skill
sandcoder skills                                   # specialists + library skills, with pins and tamper check
sandcoder panel "fix the discount bug test-first" -w examples/buggy-api -s tdd -t "python -m pytest -q"
```

- **Review before install.** `skills add` lists each skill's files and scripts and warns on risky patterns and prompt-injection-like text. It asks for approval, then pins the skill by git commit and content hash in `~/.sandcoder/skills/<name>/.source.json`. `sandcoder skills` flags any installed skill whose files changed since install.
- **Library in every sandbox.** Installed skills are mounted at `.sandcoder-skills/`, which is excluded from diffs. Specialists see a name and description index and call `load_skill(name)` when a skill fits.
- **Any skill as a specialist.** Pass a library skill in `-s` / `skills=[...]`. A wrapper adapts interactive steps ("ask the user") to unattended sandbox runs.
- **Human-only installs.** The MCP server can list skills but never install them, so an agent can't pull third-party instructions into your sandboxes.

Live check: `tdd` from mattpocock/skills (pinned at `c55ee46`) ran as a specialist on `examples/buggy-api`. It fixed the discount bug test-first in 32 s, with a passing, low-risk patch.

## Sandbox profiles

A `sandbox.toml` at the project root describes a reusable sandbox filled with skills. The website has a builder that generates one.

```toml
name = "py-backend"
image = "python:3.12-slim"
toolchain = ["pip install -q ruff"]           # baked into a cached, tagged image
setup = ["pip install -r requirements.txt"]   # per run
test = "python -m pytest -q"
specialists = ["security-auditor", "test-writer"]

[skills]
tdd = "github:mattpocock/skills/skills/engineering/tdd@c55ee46073ed923f86ce59a5eb3b6d895095d1b7"
conventions = "./skills/api-conventions"      # relative to this file
```

- `sandcoder profile init | show | install | build`: `install` reviews and pins the referenced skills, and `build` pre-builds the cached image on Token Factory.
- The CLI and `panel_run` pick up `sandbox.toml` automatically. They refuse to run if a referenced skill isn't installed at the pinned source.

## Specialist skills

Each specialist is a folder of data in `src/sandcoder/skills/<name>/`:
- `SKILL.md`: the role and method.
- `skill.toml`: tools, budgets, toolchain, and `preflight` commands. The harness runs the preflight commands itself and feeds their output to the agent, so scanners and coverage always run.

Setting `mode = "explore"` (see `feature-researcher`) adds a `PLAN.md` planner. The harness then forks one sandbox per proposed approach, runs the implementers in parallel, and ranks them on measured tests, diff size and risk. Branching stays in the harness, so the model never has to manage checkpoints.

Two harness checks keep reports honest:
- `min_findings` rejects a thin report.
- `requires_patch` downgrades a "pass" with an empty diff to "incomplete".

Add your own skills via `SANDCODER_SKILLS_PATH`. Each result reports the skill's content hash.

## CLI

```bash
sandcoder panel "pre-merge check" -w examples/buggy-api -s security-auditor,test-writer,bug-reproducer \
  -t "python -m pytest -q" --skill-task bug-reproducer=@examples/buggy-api/BUG_REPORT.md
sandcoder skills                       # list specialists
sandcoder run @examples/roman/TASK.md -w examples/roman -t "python -m unittest -q test_roman" -n 3   # single coding agent, best-of-N
sandcoder models                       # NVIDIA models on Token Factory
```

## Website

`site/` holds a static landing page (features, live-run replay, install, MCP reference), served by nginx on port 8080.
`GCP_PROJECT=<id> ./deploy/deploy-site.sh` deploys it to **Google Cloud Run** from source (Cloud Build, so no local Docker needed). It scales to zero and needs no auth.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest -q        # offline: scripted model + local backend
```

Layout:
- `src/sandcoder/`:
  - `sandbox.py` (Token Factory Sandboxes + local test backend)
  - `panel.py` (runs)
  - `agent.py`, `tools.py`, `guard.py`, `skills/`, `web.py`
  - `skillhub.py` (skill install/pinning), `profile.py` (`sandbox.toml`)
  - `mcp_server.py`, `cli.py`
- `plugin/`: Claude Code plugin.
- `.claude-plugin/marketplace.json`: plugin marketplace entry.
- `examples/`: demo projects.

MIT licensed.
