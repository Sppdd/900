import json
import sys
from pathlib import Path

import pytest

from sandcoder import CodingAgent, LocalSandbox, best_of_n
from sandcoder.llm import Reply, ToolCall
from sandcoder.sandbox import SandboxError, collect_local_files, safe_relpath

PY = sys.executable
EXAMPLE = Path(__file__).parent.parent / "examples" / "roman"
TEST_CMD = f"{PY} -m unittest -q test_roman"

SOLUTION = '''
_T = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
      (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]

def to_roman(n):
    if not isinstance(n, int) or not 1 <= n <= 3999:
        raise ValueError(n)
    out = ""
    for v, s in _T:
        while n >= v:
            out += s
            n -= v
    return out

_CANON = {to_roman(i): i for i in range(1, 4000)}

def from_roman(s):
    try:
        return _CANON[s]
    except KeyError:
        raise ValueError(s) from None
'''


class ScriptedModel:
    """Replays a fixed list of tool calls, like a deterministic LLM."""

    def __init__(self, calls):
        self.calls = list(calls)
        self.seen = []

    async def complete(self, messages, tools):
        self.seen.append(messages[-1])
        if not self.calls:
            return Reply(content="done")
        name, args = self.calls.pop(0)
        return Reply(content=None, tool_calls=[ToolCall(f"c{len(self.seen)}", name, json.dumps(args))])


@pytest.fixture
async def sb():
    s = LocalSandbox()
    yield s
    await s.close()


async def test_snapshots_are_immutable_and_branchable(sb):
    base = await sb.start("ignored", {"a.txt": (b"base", 0o644)})
    _, left = await sb.exec(base, "echo left > a.txt")
    _, right = await sb.exec(base, "echo right > a.txt")
    assert await sb.read(base, "a.txt") == b"base"
    assert (await sb.read(left, "a.txt")).strip() == b"left"
    assert (await sb.read(right, "a.txt")).strip() == b"right"

    res, same = await sb.exec(left, "rm a.txt", persist=False)
    assert res.ok and same == left
    assert (await sb.read(left, "a.txt")).strip() == b"left"


async def test_exec_timeout(sb):
    base = await sb.start("ignored")
    res, _ = await sb.exec(base, "sleep 5", timeout=0.2)
    assert res.exit_code == 124


def test_safe_relpath():
    assert safe_relpath("/workspace/src/x.py") == "src/x.py"
    assert safe_relpath("./a//b") == "a/b"
    for bad in ["../etc/passwd", "/etc/passwd", "a/../../b", "."]:
        with pytest.raises(SandboxError):
            safe_relpath(bad)


async def test_agent_solves_task_and_is_verified(sb):
    base = await sb.start("ignored", collect_local_files(EXAMPLE))
    model = ScriptedModel(
        [
            ("bash", {"command": "ls"}),
            ("run_tests", {}),
            ("checkpoint", {"name": "before"}),
            ("write_file", {"path": "roman.py", "content": "broken("}),
            ("rollback", {"name": "before"}),
            ("write_file", {"path": "roman.py", "content": SOLUTION}),
            ("run_tests", {}),
            ("finish", {"summary": "implemented roman conversions"}),
        ]
    )
    result = await CodingAgent(model, sb, test_command=TEST_CMD).run("implement roman.py", base)

    assert result.status == "passed", result.test_output
    assert result.summary == "implemented roman conversions"
    assert [s.tool for s in result.steps][-1] == "finish"
    assert "NotImplementedError" in result.steps[1].output  # tests failed before the fix
    assert "restored" in result.steps[4].output
    # The base snapshot is untouched by the agent's work.
    assert b"NotImplementedError" in await sb.read(base, "roman.py")


async def test_agent_claiming_success_is_not_trusted(sb):
    base = await sb.start("ignored", collect_local_files(EXAMPLE))
    model = ScriptedModel([("finish", {"summary": "all good, trust me"})])
    result = await CodingAgent(model, sb, test_command=TEST_CMD).run("x", base)
    assert result.status == "failed"
    assert not result.tests_passed


async def test_tool_errors_are_reported_to_model(sb):
    base = await sb.start("ignored", {"f.txt": (b"aa", 0o644)})
    model = ScriptedModel(
        [
            ("read_file", {"path": "../secret"}),
            ("edit_file", {"path": "f.txt", "old_text": "a", "new_text": "b"}),
            ("nope", {}),
            ("finish", {"summary": "x"}),
        ]
    )
    result = await CodingAgent(model, sb).run("x", base)
    outs = [s.output for s in result.steps]
    assert outs[0].startswith("error:") and "'..'" in outs[0]
    assert "found 2 times" in outs[1]
    assert "unknown tool" in outs[2]


async def test_max_steps(sb):
    base = await sb.start("ignored", collect_local_files(EXAMPLE))
    model = ScriptedModel([("bash", {"command": "true"})] * 10)
    result = await CodingAgent(model, sb, test_command=TEST_CMD, max_steps=3).run("x", base)
    assert result.status == "max_steps"
    assert len(result.steps) == 3


async def test_best_of_n_picks_passing_branch(sb, tmp_path):
    base = await sb.start("ignored", collect_local_files(EXAMPLE))
    scripts = {
        0: [("write_file", {"path": "roman.py", "content": "def to_roman(n): return ''"}), ("finish", {"summary": "bad"})],
        1: [("write_file", {"path": "roman.py", "content": SOLUTION}), ("finish", {"summary": "good"})],
    }

    def make(i):
        return CodingAgent(ScriptedModel(scripts[i]), sb, test_command=TEST_CMD)

    best, results = await best_of_n(make, "x", base, n=2, stop_on_first_pass=False)
    assert best.summary == "good" and best.tests_passed
    assert len(results) == 2

    out = await sb.export(best.snapshot, tmp_path / "out")
    assert "_CANON" in (out / "roman.py").read_text()
