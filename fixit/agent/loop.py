"""The agent loop: LLM proposes tool calls -> we run them -> feed results back -> repeat.

Bounded by MAX_ITERATIONS. Outcome is one of:
  success  - agent stopped calling tools and the last run_tests reported PASSED
  failed   - agent stopped but tests never passed
  aborted  - iteration cap hit
  error    - exception (LLMError after retries, or anything unexpected)
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import structlog

from agent import metrics

log = structlog.get_logger()
from agent.llm import LLM, LLMError, LLMResponse, complete_with_retry, cost_usd
from agent.sandbox import Sandbox
from agent.tools import TOOL_SCHEMAS, Toolbox

MAX_ITERATIONS = 10
MAX_NUDGES = 4  # times we push back when the model narrates instead of calling a tool
MAX_EMPTY = 3   # empty replies (small models sometimes emit nothing) answered with "Continue."

SYSTEM_PROMPT = (
    "You are fixit, an automated coding agent working inside a copy of a Python repository. "
    "You have three tools: read_file, write_file, run_tests. Work in small steps: run the tests first "
    "to see what fails, read only the files you need, make the smallest change that fixes the failure, "
    "then run the tests again. Act by calling tools, one step at a time; never just describe what you "
    "are going to do. Never guess what a file contains: call read_file and work from the real content. "
    "write_file replaces the whole file, so pass the complete new content. When run_tests reports "
    "PASSED, stop calling tools and reply with a short summary of what you changed. Do not modify "
    "test files unless the task explicitly asks you to."
)
NUDGE_DONE = (
    "Your reply contained no tool call. If the task is finished reply with exactly: DONE. "
    "Otherwise call one tool now."
)


WRITE_SHAPE = '{"name": "write_file", "arguments": {"path": "<file>", "content": "<complete new file content>"}}'
RUN_SHAPE = '{"name": "run_tests", "arguments": {}}'


def nudge_for(last_tool: str | None, tests_passed: bool) -> str:
    """State-aware push-back when the model narrates instead of acting. Shows the literal
    call shape because small models follow an example far better than an instruction."""
    if tests_passed:
        return NUDGE_DONE
    if last_tool == "read_file":
        return f"You described the change but did not call write_file. Reply with ONLY the tool call, nothing else: {WRITE_SHAPE}"
    if last_tool == "write_file":
        return f"Now run the tests. Reply with ONLY the tool call: {RUN_SHAPE}"
    if last_tool == "run_tests":
        return (f"The tests still fail (see the assertion above). Apply your fix by calling write_file with the "
                f"COMPLETE corrected file. Reply with ONLY the tool call: {WRITE_SHAPE}")
    return f"Your reply contained no tool call. Reply with ONLY a tool call, e.g. {RUN_SHAPE}"


@dataclass
class TaskResult:
    run_id: str
    outcome: str
    iterations: int
    duration_s: float
    tokens_in: int
    tokens_out: int
    cost_usd: float
    summary: str
    changed_files: list[str] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _assistant_message(resp: LLMResponse) -> dict:
    content: list[dict] = []
    if resp.text:
        content.append({"type": "text", "text": resp.text})
    for c in resp.tool_calls:
        content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
    return {"role": "assistant", "content": content}


def run_task(run_id: str, task: str, repo_path: str, llm: LLM) -> TaskResult:
    start = time.perf_counter()
    iterations = 0
    tokens_in = tokens_out = 0
    outcome = "aborted"
    summary = ""
    changed: list[str] = []
    error = error_type = None
    tests_passed = False
    nudges = 0
    empties = 0
    last_tool: str | None = None

    structlog.contextvars.bind_contextvars(run_id=run_id)
    log.info("task_started", msg="task started", task=task[:120], repo=str(repo_path), llm=llm.provider)
    metrics.TASKS_IN_PROGRESS.inc()
    metrics.record_demo_request(run_id)  # Part E.2 only; no-op label-wise unless enabled
    sandbox = Sandbox(run_id, repo_path)
    try:
        sandbox.create()
        tools = Toolbox(sandbox)
        listing = ", ".join(sorted(str(f.relative_to(sandbox.workdir)) for f in sandbox._files())[:40])
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{task}\n\nFiles in the repository: {listing}"},
        ]
        while iterations < MAX_ITERATIONS:
            iterations += 1
            structlog.contextvars.bind_contextvars(iteration=iterations)
            resp = complete_with_retry(llm, messages, TOOL_SCHEMAS)
            tokens_in += resp.input_tokens
            tokens_out += resp.output_tokens
            messages.append(_assistant_message(resp))
            if not resp.tool_calls:
                text = (resp.text or "").strip()
                if not text and empties < MAX_EMPTY and iterations < MAX_ITERATIONS:
                    empties += 1
                    log.info("empty_reply", msg="model returned nothing; asking it to continue", empties=empties)
                    messages.append({"role": "user", "content": "Continue."})
                    continue
                if not tests_passed and text != "DONE" and nudges < MAX_NUDGES and iterations < MAX_ITERATIONS:
                    nudges += 1
                    log.info("nudge", msg="model narrated instead of calling a tool; asking it to act", nudge=nudges, last_tool=last_tool)
                    messages.append({"role": "user", "content": nudge_for(last_tool, tests_passed)})
                    continue
                summary = text
                outcome = "success" if tests_passed else "failed"
                break
            results = []
            for call in resp.tool_calls:
                text, status = tools.dispatch(call.name, call.input)
                metrics.TOOL_CALLS.labels(tool=call.name, status=status).inc()
                last_tool = call.name
                if call.name == "run_tests":
                    tests_passed = tools.last_test_result is not None and tools.last_test_result.result == "passed"
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": status == "error"})
            messages.append({"role": "user", "content": results})
        if outcome == "aborted":
            summary = f"stopped after {iterations} iterations without finishing"
        if outcome == "success":
            changed = sandbox.commit()
    except LLMError as e:
        outcome, error, error_type = "error", str(e), "LLMError"
        summary = f"LLM unavailable: {e}"
        log.error("error", msg="LLM unavailable after retries", exc_type="LLMError", exc_message=str(e)[:200])
    except Exception as e:  # noqa: BLE001
        outcome, error, error_type = "error", str(e), type(e).__name__
        summary = f"internal error: {type(e).__name__}: {e}"
        log.error("error", msg="unexpected exception in agent loop", exc_type=type(e).__name__, exc_message=str(e)[:200], exc_info=True)
    finally:
        sandbox.cleanup()
        metrics.TASKS_IN_PROGRESS.dec()

    duration = time.perf_counter() - start
    metrics.TASKS_TOTAL.labels(outcome=outcome).inc()
    metrics.TASK_DURATION.labels(outcome=outcome).observe(duration)
    metrics.TASK_ITERATIONS.labels(outcome=outcome).observe(iterations)
    total_cost = cost_usd(llm.model, tokens_in, tokens_out)
    log.info("task_finished", msg=f"task {outcome}", outcome=outcome, iterations=iterations,
             duration_ms=int(duration * 1000), cost_usd=round(total_cost, 6), tokens_in=tokens_in, tokens_out=tokens_out,
             changed_files=len(changed))
    structlog.contextvars.unbind_contextvars("run_id", "iteration")
    return TaskResult(
        run_id=run_id,
        outcome=outcome,
        iterations=iterations,
        duration_s=round(duration, 3),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(total_cost, 6),
        summary=summary,
        changed_files=changed,
        error=error,
        error_type=error_type,
    )
