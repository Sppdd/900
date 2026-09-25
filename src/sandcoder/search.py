"""Best-of-N: fork N agents from one snapshot, keep the attempt that passes the tests.

Because snapshots are immutable, every attempt starts from the exact same
state (including any expensive setup), and losing branches cost nothing to
discard.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sandcoder.agent import AgentResult, CodingAgent
from sandcoder.sandbox import Snapshot

AttemptFactory = Callable[[int], CodingAgent]


def rank(result: AgentResult) -> tuple[int, int]:
    return (0 if result.tests_passed else 1, len(result.steps))


async def best_of_n(
    make_agent: AttemptFactory,
    task: str,
    base: Snapshot,
    n: int = 3,
    *,
    stop_on_first_pass: bool = True,
    on_done: Callable[[int, AgentResult], None] | None = None,
) -> tuple[AgentResult, list[AgentResult | BaseException]]:
    """Run ``n`` attempts concurrently from ``base``; return (best, all results)."""
    agents: list[CodingAgent] = [make_agent(i) for i in range(n)]
    tasks = [asyncio.create_task(a.run(task, base)) for a in agents]
    results: list[AgentResult | BaseException | None] = [None] * n
    index = {t: i for i, t in enumerate(tasks)}

    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            i = index[t]
            exc = t.exception()
            results[i] = exc if exc is not None else t.result()
            if on_done and exc is None:
                on_done(i, t.result())
        if stop_on_first_pass and any(isinstance(r, AgentResult) and r.tests_passed for r in results):
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in pending:
                results[index[t]] = asyncio.CancelledError("stopped: another attempt passed")
            break

    finished = [r for r in results if isinstance(r, AgentResult)]
    if not finished:
        errors = [r for r in results if isinstance(r, BaseException)]
        raise RuntimeError(f"all {n} attempts crashed; first error: {errors[0]!r}") from errors[0]
    best = min(finished, key=rank)
    return best, [r for r in results if r is not None]
