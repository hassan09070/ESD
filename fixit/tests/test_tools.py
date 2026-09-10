import pytest

from agent.sandbox import Sandbox
from agent.tools import Toolbox


@pytest.fixture
def toolbox(broken_repo):
    sb = Sandbox("tools00000001", str(broken_repo))
    sb.create()
    yield Toolbox(sb)
    sb.cleanup()


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "a/../../x", ""])
def test_read_refuses_traversal(toolbox, path):
    text, status = toolbox.dispatch("read_file", {"path": path})
    assert status == "error" and text.startswith("ERROR")


@pytest.mark.parametrize("path", ["../evil.py", "/tmp/evil.py"])
def test_write_refuses_traversal(toolbox, path):
    text, status = toolbox.dispatch("write_file", {"path": path, "content": "x"})
    assert status == "error"


def test_read_write_roundtrip(toolbox):
    assert toolbox.dispatch("write_file", {"path": "new.txt", "content": "hi"})[1] == "ok"
    assert toolbox.dispatch("read_file", {"path": "new.txt"}) == ("hi", "ok")


def test_run_tests_reports_failed_then_passed(toolbox):
    text, status = toolbox.dispatch("run_tests", {})
    assert status == "ok" and "FAILED" in text
    from agent.llm import FIXED_CALCULATOR
    toolbox.dispatch("write_file", {"path": "calculator.py", "content": FIXED_CALCULATOR})
    text, _ = toolbox.dispatch("run_tests", {})
    assert "PASSED" in text


def test_sandbox_timeout_returns_timeout(broken_repo):
    (broken_repo / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(5)\n")
    sb = Sandbox("timeout0000001", str(broken_repo))
    sb.create()
    try:
        r = sb.run_pytest(timeout_s=1)
        assert r.result == "timeout" and r.marker == "TIMEOUT"
    finally:
        sb.cleanup()


def test_unknown_tool(toolbox):
    assert toolbox.dispatch("rm_rf", {})[1] == "error"
