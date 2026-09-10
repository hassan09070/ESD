import time

from prometheus_client import REGISTRY

from agent.llm import FaultInjectingLLM, LLMError, LLMResponse, MockLLM, ToolCall, complete_with_retry
from agent.loop import MAX_ITERATIONS, run_task


def _val(name, labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_slow_mode_delays_every_fifth_call():
    llm = FaultInjectingLLM(MockLLM(latency=0), mode="slow", delay_s=0.3)
    durations = []
    for _ in range(5):
        t0 = time.perf_counter()
        llm.complete([{"role": "user", "content": "x"}], [])
        durations.append(time.perf_counter() - t0)
    assert max(durations[:4]) < 0.1 and durations[4] >= 0.3


def test_flaky_mode_raises_every_fifth_call_and_retry_metric_increments(monkeypatch):
    monkeypatch.setattr("agent.llm.BACKOFF_BASE_S", 0.0)
    llm = FaultInjectingLLM(MockLLM(latency=0), mode="flaky")
    before_retry = _val("fixit_llm_requests_total", {"provider": "mock", "status": "retry"})
    before_err = _val("fixit_llm_requests_total", {"provider": "mock", "status": "error"})
    for _ in range(5):  # 5th call raises inside complete_with_retry, 6th succeeds
        complete_with_retry(llm, [{"role": "user", "content": "x"}], [])
    assert _val("fixit_llm_requests_total", {"provider": "mock", "status": "retry"}) == before_retry + 1
    assert _val("fixit_llm_requests_total", {"provider": "mock", "status": "error"}) == before_err + 1


def test_unknown_mode_rejected():
    import pytest
    with pytest.raises(ValueError):
        FaultInjectingLLM(MockLLM(latency=0), mode="explode")


class LoopingLLM:
    """Never finishes: keeps asking for run_tests."""
    provider = "mock"
    model = "claude-sonnet-4-6"

    def complete(self, messages, tools):
        return LLMResponse(text=None, tool_calls=[ToolCall("t", "run_tests", {})], input_tokens=10, output_tokens=1)


def test_iteration_cap_gives_aborted(broken_repo):
    result = run_task("cap0000000001", "fix", str(broken_repo), LoopingLLM())
    assert result.outcome == "aborted" and result.iterations == MAX_ITERATIONS
    assert "BUG" in (broken_repo / "calculator.py").read_text()  # nothing written back


class GiveUpLLM:
    """Stops without fixing anything -> failed."""
    provider = "mock"
    model = "claude-sonnet-4-6"

    def complete(self, messages, tools):
        return LLMResponse(text="I cannot fix this.", tool_calls=[], input_tokens=10, output_tokens=1)


def test_stopping_without_passing_tests_is_failed(broken_repo):
    result = run_task("fail000000001", "fix", str(broken_repo), GiveUpLLM())
    assert result.outcome == "failed" and result.iterations == 1
