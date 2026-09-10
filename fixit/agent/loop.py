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

from agent.llm import LLM, LLMError, LLMResponse, complete_with_retry, cost_usd
from agent.sandbox import Sandbox
from agent.tools import TOOL_SCHEMAS, Toolbox

MAX_ITERATIONS = 10

SYSTEM_PROMPT = (
    "You are fixit, an automated coding agent working inside a copy of a Python repository. "
    "You have three tools: read_file, write_file, run_tests. Work in small steps: run the tests first "
    "to see what fails, read only the files you need, make the smallest change that fixes the failure, "
    "then run the tests again. When run_tests reports PASSED, stop calling tools and reply with a short "
    "summary of what you changed. Do not modify test files unless the task explicitly asks you to."
)


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

    sandbox = Sandbox(run_id, repo_path)
    try:
        sandbox.create()
        tools = Toolbox(sandbox)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        while iterations < MAX_ITERATIONS:
            iterations += 1
            resp = complete_with_retry(llm, messages, TOOL_SCHEMAS)
            tokens_in += resp.input_tokens
            tokens_out += resp.output_tokens
            messages.append(_assistant_message(resp))
            if not resp.tool_calls:
                summary = resp.text or ""
                outcome = "success" if tests_passed else "failed"
                break
            results = []
            for call in resp.tool_calls:
                text, status = tools.dispatch(call.name, call.input)
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
    except Exception as e:  # noqa: BLE001
        outcome, error, error_type = "error", str(e), type(e).__name__
        summary = f"internal error: {type(e).__name__}: {e}"
    finally:
        sandbox.cleanup()

    duration = time.perf_counter() - start
    return TaskResult(
        run_id=run_id,
        outcome=outcome,
        iterations=iterations,
        duration_s=round(duration, 3),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(cost_usd(llm.model, tokens_in, tokens_out), 6),
        summary=summary,
        changed_files=changed,
        error=error,
        error_type=error_type,
    )
