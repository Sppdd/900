"""The coding agent loop: model proposes tool calls, the sandbox executes them."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sandcoder.llm import ChatModel
from sandcoder.sandbox import Sandbox, Snapshot
from sandcoder.tools import HostTool, Workspace, clip, schemas_for

SYSTEM_PROMPT = """\
You are an autonomous software engineer working inside an isolated Linux sandbox.
The project is in your current working directory; use paths relative to it.

Work loop:
1. Explore the project briefly (list files, read the relevant code and tests).
2. Make focused changes with write_file / edit_file.
3. Run the tests with run_tests (or bash) and read failures carefully.
4. Iterate until the tests pass, then call finish with a short summary.

Rules:
- Every tool call runs in the sandbox; nothing you do touches the user's machine.
- Before a risky change, call checkpoint; use rollback if an approach fails.
- Do not modify tests to make them pass unless the task explicitly asks for it.
- Keep commands non-interactive and bounded in time.
"""


@dataclass
class Step:
    tool: str
    arguments: str
    output: str
    snapshot: str
    seconds: float


@dataclass
class AgentResult:
    status: str  # "passed" | "failed" | "max_steps"
    summary: str
    snapshot: Snapshot
    tests_passed: bool
    test_output: str
    steps: list[Step] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "snapshot": self.snapshot.id,
            "tests_passed": self.tests_passed,
            "test_output": self.test_output,
            "usage": self.usage,
            "report": self.report,
            "audit": self.audit,
            "steps": [s.__dict__ for s in self.steps],
        }


class CodingAgent:
    def __init__(
        self,
        model: ChatModel,
        sandbox: Sandbox,
        *,
        test_command: str | None = None,
        max_steps: int = 40,
        system_prompt: str = SYSTEM_PROMPT,
        on_step: Callable[[Step], None] | None = None,
        tools: list[str] | None = None,
        host_tools: dict[str, HostTool] | None = None,
        use_guard: bool = True,
    ) -> None:
        self.model = model
        self.sandbox = sandbox
        self.test_command = test_command
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.on_step = on_step
        self.tools = tools  # None = all built-in tools; a list enables a subset (+ "report")
        self.host_tools = host_tools or {}
        self.use_guard = use_guard
        self.schemas = schemas_for(tools, self.host_tools)
        self.end_tool = "report" if tools is not None and "report" in tools else "finish"

    async def run(self, task: str, snapshot: Snapshot) -> AgentResult:
        ws = Workspace(
            self.sandbox, snapshot, test_command=self.test_command,
            host_tools=self.host_tools, use_guard=self.use_guard,
        )
        user = f"Task:\n{task}"
        if self.test_command:
            user += f"\n\nThe task is verified by running: `{self.test_command}`"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]
        steps: list[Step] = []
        usage: dict[str, int] = {}
        summary = ""
        finished = False

        for _ in range(self.max_steps):
            reply = await self.model.complete(messages, self.schemas)
            for k, v in reply.usage.items():
                usage[k] = usage.get(k, 0) + v
            messages.append(reply.as_message())

            if not reply.tool_calls:
                # Models sometimes answer in prose; nudge back into the tool loop.
                messages.append({"role": "user", "content": f"Continue using tools, or call {self.end_tool} when done."})
                continue

            for call in reply.tool_calls:
                t0 = time.monotonic()
                output = await ws.call(call.name, call.arguments)
                step = Step(call.name, call.arguments, output, ws.snapshot.id, time.monotonic() - t0)
                steps.append(step)
                if self.on_step:
                    self.on_step(step)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                if call.name == "finish" or (call.name == "report" and ws.report is not None):
                    finished = True
                    summary = ws.report["summary"] if ws.report else _arg(call.arguments, "summary")

            if finished:
                break

        # Never trust the agent's own claim: verify independently on the final snapshot.
        passed, test_output = await ws.run_tests()
        status = "passed" if passed else ("failed" if finished else "max_steps")
        return AgentResult(
            status=status,
            summary=summary,
            snapshot=ws.snapshot,
            tests_passed=passed,
            test_output=clip(test_output),
            steps=steps,
            usage=usage,
            report=ws.report,
            audit=ws.audit,
            evidence=ws.evidence,
        )


def _arg(raw: str, key: str) -> str:
    try:
        return str(json.loads(raw).get(key, ""))
    except (ValueError, AttributeError):
        return ""
