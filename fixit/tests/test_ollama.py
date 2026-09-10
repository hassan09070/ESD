import json

import httpx
import pytest

from agent.llm import LLMError, OllamaLLM, _to_ollama_messages, _to_ollama_tools, cost_usd
from agent.tools import TOOL_SCHEMAS


def test_message_conversion_maps_tool_blocks():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [{"type": "text", "text": "running"},
                                          {"type": "tool_use", "id": "t1", "name": "run_tests", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "FAILED"}]},
    ]
    out = _to_ollama_messages(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"]["name"] == "run_tests"
    assert out[3]["content"] == "FAILED"


def test_tool_schema_conversion():
    tools = _to_ollama_tools(TOOL_SCHEMAS)
    assert {t["function"]["name"] for t in tools} == {"read_file", "write_file", "run_tests"}
    assert tools[0]["type"] == "function" and "parameters" in tools[0]["function"]


def _client(handler):
    return OllamaLLM(model="fake:1b", base_url="http://ollama.test", transport=httpx.MockTransport(handler))


def test_parses_tool_calls_and_tokens_and_string_arguments():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["model"] == "fake:1b" and body["stream"] is False and body["tools"]
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": "write_file",
                                                     "arguments": json.dumps({"path": "a.py", "content": "x"})}}]},
            "prompt_eval_count": 123, "eval_count": 45,
        })
    resp = _client(handler).complete([{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert resp.tool_calls[0].name == "write_file" and resp.tool_calls[0].input == {"path": "a.py", "content": "x"}
    assert resp.tool_calls[0].id.startswith("toolu_ollama_")
    assert (resp.input_tokens, resp.output_tokens) == (123, 45) and resp.text is None


def test_final_text_without_tools():
    def handler(request):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "done"}, "prompt_eval_count": 1, "eval_count": 1})
    resp = _client(handler).complete([{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert resp.text == "done" and resp.tool_calls == []


def test_missing_model_and_server_errors_are_llmerror():
    with pytest.raises(LLMError, match="ollama pull"):
        _client(lambda r: httpx.Response(404, json={"error": "model not found"})).complete([], [])
    with pytest.raises(LLMError, match="status 500"):
        _client(lambda r: httpx.Response(500, text="boom")).complete([], [])

    def down(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(LLMError, match="connection"):
        _client(down).complete([], [])


def test_local_models_cost_nothing():
    assert cost_usd("qwen2.5-coder:7b", 10_000, 10_000) == 0.0
    assert cost_usd("claude-sonnet-4-6", 1_000_000, 0) == 3.0


@pytest.mark.parametrize("content", [
    '{"name": "run_tests", "arguments": {}}',
    'Let\'s start by running the tests.\n\n{"name": "run_tests", "arguments": {}}',
    '{"name": "run_tests", "arguments": "{}"}',
    '<tool_call>\n{"name": "run_tests", "arguments": {}}\n</tool_call>',
    'Let me run the tests.\n```json\n{"name": "run_tests", "arguments": {}}\n```',
    '[{"name": "run_tests", "parameters": {}}]',
])
def test_tool_call_written_as_text_is_recovered(content):
    def handler(request):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, "prompt_eval_count": 1, "eval_count": 1})
    resp = _client(handler).complete([{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert [c.name for c in resp.tool_calls] == ["run_tests"]
    assert resp.text is None or "run_tests" not in resp.text


def test_prose_json_with_unknown_name_stays_text():
    def handler(request):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"name": "explode", "arguments": {}}'}, "prompt_eval_count": 1, "eval_count": 1})
    resp = _client(handler).complete([{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert resp.tool_calls == [] and resp.text is not None


def test_write_file_call_in_text_keeps_content_argument():
    payload = '{"name": "write_file", "arguments": {"path": "calculator.py", "content": "def add(a, b):\\n    return a + b\\n"}}'
    def handler(request):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "Fixing.\n" + payload}, "prompt_eval_count": 1, "eval_count": 1})
    resp = _client(handler).complete([{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert resp.tool_calls[0].input["path"] == "calculator.py" and "return a + b" in resp.tool_calls[0].input["content"]
    assert resp.text == "Fixing."


def test_special_tokens_are_stripped():
    def handler(request):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "<|im_start|>"}, "prompt_eval_count": 1, "eval_count": 1})
    resp = _client(handler).complete([{"role": "user", "content": "hi"}], TOOL_SCHEMAS)
    assert resp.text is None and resp.tool_calls == []
