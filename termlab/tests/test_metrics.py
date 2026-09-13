import re

from prometheus_client import REGISTRY

from api import metrics


def exposition() -> str:
    body, ctype = metrics.render()
    assert ctype.startswith("text/plain")
    return body.decode()


def test_all_four_metric_types_present():
    text = exposition()
    types = {m.group(2) for m in re.finditer(r"^# TYPE (termlab_\w+) (\w+)$", text, re.M)}
    assert {"counter", "gauge", "histogram", "summary"} <= types


def test_every_metric_is_prefixed():
    names = {m.group(1) for m in re.finditer(r"^# TYPE (\w+) ", exposition(), re.M)}
    ours = {n for n in names if not n.startswith(("python_", "process_"))}
    assert ours and all(n.startswith("termlab_") for n in ours)


def test_enums_are_pre_initialised_so_rates_start_at_zero():
    for o in metrics.OUTCOMES:
        assert REGISTRY.get_sample_value("termlab_sessions_started_total", {"outcome": o}) is not None
    for r in metrics.REAP_REASONS:
        assert REGISTRY.get_sample_value("termlab_sandboxes_reaped_total", {"reason": r}) is not None


def test_demo_counter_has_no_label_by_default():
    assert not metrics.DEMO_CARDINALITY_ENABLED
    before = REGISTRY.get_sample_value("termlab_demo_requests_total") or 0
    metrics.record_demo_request("abc")
    metrics.record_demo_request("def")
    assert REGISTRY.get_sample_value("termlab_demo_requests_total") == before + 2
    assert "request_id=" not in exposition()


def test_no_high_cardinality_labels_in_exposition():
    text = exposition()
    for forbidden in ("session_id=", "container_id=", "sandbox_id=", "token="):
        assert forbidden not in text
