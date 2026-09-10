from agent.llm import FaultInjectingLLM, LLMError, MockLLM
from agent.loop import run_task


def test_mock_llm_reaches_success_within_5_iterations(broken_repo):
    result = run_task("test000000001", "make the failing tests pass", str(broken_repo), MockLLM(latency=0))
    assert result.outcome == "success"
    assert result.iterations <= 5
    assert result.changed_files == ["calculator.py"]
    assert result.cost_usd > 0
    assert "b - a" not in (broken_repo / "calculator.py").read_text()


def test_flaky_llm_retries_and_never_crashes(broken_repo):
    llm = FaultInjectingLLM(MockLLM(latency=0), mode="flaky")
    outcomes = set()
    for i in range(3):
        result = run_task(f"flaky{i:08d}", "fix", str(broken_repo), llm)
        outcomes.add(result.outcome)
    assert outcomes <= {"success", "error"}
    assert "success" in outcomes


class AlwaysFailingLLM:
    provider = "mock"
    model = "claude-sonnet-4-6"

    def complete(self, messages, tools):
        raise LLMError("down")


def test_llm_error_after_retries_is_reported_not_raised(broken_repo, monkeypatch):
    monkeypatch.setattr("agent.llm.BACKOFF_BASE_S", 0.0)
    result = run_task("err000000001", "fix", str(broken_repo), AlwaysFailingLLM())
    assert result.outcome == "error"
    assert result.error_type == "LLMError"
