"""LLM abstraction: AnthropicLLM, OllamaLLM (local), MockLLM (scripted), FaultInjectingLLM (Part E switch).

All three expose the same `complete(messages, tools) -> LLMResponse` method. `build_llm()`
picks the backend from FIXIT_LLM and wraps it in FaultInjectingLLM using FIXIT_FAULT.
`complete_with_retry()` is the single entry point the loop uses; it retries LLMError with
backoff so a flaky provider does not immediately kill a task.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from agent import metrics

log = structlog.get_logger()

MODEL = "claude-sonnet-4-6"

# USD per million tokens. MockLLM uses the same table so cost metrics are non-zero.
PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """USD for a call. Models not in the table (local Ollama models) cost 0."""
    price = PRICING_USD_PER_MTOK.get(model)
    if price is None:
        return 0.0
    return tokens_in / 1e6 * price["input"] + tokens_out / 1e6 * price["output"]


class LLMError(Exception):
    """The provider failed (network, 5xx, injected fault). Retryable."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    provider: str
    model: str

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


# --------------------------------------------------------------------------- Anthropic
class AnthropicLLM:
    """Real client. Messages API with tool use. The first message may be a `system` role
    entry; it is lifted into the top-level `system` parameter."""

    provider = "anthropic"

    def __init__(self, model: str = MODEL, api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or None
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            import anthropic

            # max_retries=0: the loop owns retries so they show up in our metrics/logs.
            self._client = anthropic.Anthropic(api_key=self._api_key, max_retries=0, timeout=120.0)
        return self._client

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        import anthropic

        client = self._get_client()
        system = None
        if messages and messages[0]["role"] == "system":
            system = messages[0]["content"]
            messages = messages[1:]
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system or anthropic.NOT_GIVEN,
                tools=tools,
                messages=messages,
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"anthropic status {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"anthropic connection error: {e}") from e

        text_parts = [b.text for b in resp.content if b.type == "text"]
        calls = [ToolCall(id=b.id, name=b.name, input=dict(b.input)) for b in resp.content if b.type == "tool_use"]
        return LLMResponse(
            text="\n".join(text_parts) or None,
            tool_calls=calls,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )


# --------------------------------------------------------------------------- Ollama (local)
OLLAMA_DEFAULT_URL = "http://host.docker.internal:11434"  # the host's Ollama, as seen from the container
OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:7b"


def _to_ollama_messages(messages: list[dict]) -> list[dict]:
    """Convert our Anthropic-shaped transcript into Ollama /api/chat messages.

    assistant tool_use blocks -> assistant.tool_calls; user tool_result blocks -> role=tool
    messages (one per result, in order). Plain strings pass through unchanged."""
    out: list[dict] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [
                {"function": {"name": b["name"], "arguments": b.get("input", {})}}
                for b in content if b.get("type") == "tool_use"
            ]
            msg: dict = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user message carrying tool results
            for b in content:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "content": str(b.get("content", ""))})
                elif b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def _to_ollama_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool schema (name/description/input_schema) -> OpenAI-style function tools."""
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                          "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
        for t in tools
    ]


_TOOL_CALL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_SPECIAL_TOKEN = re.compile(r"<\|[a-z_]+\|>")  # e.g. <|im_start|>, leaked by small models


def _extract_text_tool_calls(text: str | None, tool_names: set[str]) -> tuple[list[dict], str | None]:
    """Small models sometimes write the call into the text instead of `tool_calls`, e.g.
    `{"name": "run_tests", "arguments": {}}` (possibly after some prose, inside a code
    fence, or in <tool_call> tags). Scan the text for JSON objects, keep those whose
    `name` is a known tool, and return (calls, remaining_prose)."""
    if not text:
        return [], text
    decoder = json.JSONDecoder()
    calls: list[dict] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while True:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, j)
        except json.JSONDecodeError:
            i = j + 1
            continue
        items = obj if isinstance(obj, list) else [obj]
        found = False
        for item in items:
            if isinstance(item, dict) and item.get("name") in tool_names:
                args = item.get("arguments", item.get("parameters", item.get("input", {})))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append({"function": {"name": item["name"], "arguments": args if isinstance(args, dict) else {}}})
                found = True
        if found:
            spans.append((j, end))
        i = end if found else j + 1
    if not calls:
        return [], text
    rest = text
    for a, b in reversed(spans):
        rest = rest[:a] + rest[b:]
    rest = _TOOL_CALL_BLOCK.sub("", rest)
    rest = re.sub(r"```(?:json)?\s*```", "", rest).strip()
    return calls, (rest or None)


class OllamaLLM:
    """Local model through Ollama's native /api/chat endpoint with tool calling.

    Runs on the host (Metal-accelerated on a Mac); the agent container reaches it at
    host.docker.internal:11434. Token counts come from prompt_eval_count/eval_count and
    cost is always $0. Small models sometimes return tool arguments as a JSON string,
    which is tolerated. Any HTTP/connection failure is raised as LLMError (retryable)."""

    provider = "ollama"

    def __init__(self, model: str | None = None, base_url: str | None = None, num_ctx: int | None = None,
                 timeout_s: float = 600.0, transport=None):
        self.model = model or os.environ.get("OLLAMA_MODEL", OLLAMA_DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("OLLAMA_URL", OLLAMA_DEFAULT_URL)).rstrip("/")
        self.num_ctx = num_ctx or int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
        self.timeout_s = timeout_s
        self._transport = transport  # tests inject httpx.MockTransport
        self._calls = 0
        self._lock = threading.Lock()

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        import httpx

        body = {
            "model": self.model,
            "messages": _to_ollama_messages(messages),
            "tools": _to_ollama_tools(tools),
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx, "stop": ["<|im_start|>"]},
        }
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout_s) as client:
                resp = client.post(f"{self.base_url}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"ollama connection error: {e}") from e
        if resp.status_code == 404:
            raise LLMError(f"ollama: model {self.model!r} not found - run `ollama pull {self.model}`")
        if resp.status_code >= 400:
            raise LLMError(f"ollama status {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        msg = data.get("message", {})
        raw_calls = msg.get("tool_calls", []) or []
        content = _SPECIAL_TOKEN.sub("", msg.get("content") or "")
        if not raw_calls:
            raw_calls, content = _extract_text_tool_calls(content, {t["name"] for t in tools})
        calls: list[ToolCall] = []
        for tc in raw_calls:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            with self._lock:
                self._calls += 1
                n = self._calls
            calls.append(ToolCall(id=tc.get("id") or f"toolu_ollama_{n}", name=fn.get("name", ""), input=args or {}))
        text = (content or "").strip() or None
        return LLMResponse(
            text=text,
            tool_calls=calls,
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
        )


# --------------------------------------------------------------------------- Mock
FIXED_CALCULATOR = '''"""A tiny calculator module used as the fixit sample repo."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''


class MockLLM:
    """Deterministic scripted agent for the sample repo.

    Step is derived from how many assistant turns are already in `messages`, so one
    MockLLM instance can serve many concurrent tasks. Each call sleeps `latency`
    seconds (default 0.2 s) so latency histograms have a realistic shape.
    """

    provider = "mock"
    TOKENS_IN = 1500
    TOKENS_OUT = 200

    def __init__(self, model: str = MODEL, latency: float = 0.2):
        self.model = model
        self.latency = latency

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if self.latency:
            time.sleep(self.latency)
        step = sum(1 for m in messages if m.get("role") == "assistant")
        script = [
            ("run_tests", {}),
            ("read_file", {"path": "calculator.py"}),
            ("write_file", {"path": "calculator.py", "content": FIXED_CALCULATOR}),
            ("run_tests", {}),
        ]
        if step < len(script):
            name, args = script[step]
            return LLMResponse(
                text=f"Step {step + 1}: calling {name}.",
                tool_calls=[ToolCall(id=f"toolu_mock_{step}", name=name, input=args)],
                input_tokens=self.TOKENS_IN,
                output_tokens=self.TOKENS_OUT,
            )
        return LLMResponse(
            text="Fixed subtract (arguments were swapped) and divide (off-by-one). All tests pass.",
            tool_calls=[],
            input_tokens=self.TOKENS_IN,
            output_tokens=self.TOKENS_OUT,
        )


# --------------------------------------------------------------------------- Fault injection
class FaultInjectingLLM:
    """Part E.1 fault switch. mode=none passes through; slow adds +3 s to every 5th call;
    flaky raises LLMError on every 5th call. The call counter is process-wide."""

    def __init__(self, inner: LLM, mode: str = "none", every: int = 5, delay_s: float = 3.0):
        if mode not in ("none", "slow", "flaky"):
            raise ValueError(f"unknown FIXIT_FAULT mode {mode!r}")
        self.inner = inner
        self.mode = mode
        self.every = every
        self.delay_s = delay_s
        self.provider = inner.provider
        self.model = inner.model
        self._calls = 0
        self._lock = threading.Lock()

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        with self._lock:
            self._calls += 1
            n = self._calls
        if self.mode != "none" and n % self.every == 0:
            if self.mode == "slow":
                time.sleep(self.delay_s)
            elif self.mode == "flaky":
                raise LLMError("injected fault: simulated provider outage")
        return self.inner.complete(messages, tools)


# --------------------------------------------------------------------------- Retry + selection
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.5


def complete_with_retry(llm: LLM, messages: list[dict], tools: list[dict]) -> LLMResponse:
    """Call the LLM; on LLMError retry up to MAX_ATTEMPTS with exponential backoff.

    Metrics recorded here (every attempt): fixit_llm_requests_total{status=ok|error|retry},
    fixit_llm_request_duration_seconds, fixit_llm_tokens_total, fixit_llm_cost_usd_total.
    """
    provider = llm.provider
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            resp = llm.complete(messages, tools)
        except LLMError as e:
            duration = time.perf_counter() - start
            metrics.LLM_DURATION.labels(provider=provider).observe(duration)
            metrics.LLM_REQUESTS.labels(provider=provider, status="error").inc()
            log.error("llm_call", msg="LLM call failed", provider=provider, duration_ms=int(duration * 1000),
                        status="error", attempt=attempt, exc_type=type(e).__name__, exc_message=str(e)[:200])
            if attempt == MAX_ATTEMPTS:
                raise
            backoff = BACKOFF_BASE_S * 2 ** (attempt - 1)
            metrics.LLM_REQUESTS.labels(provider=provider, status="retry").inc()
            log.warning("llm_retry", msg=f"retrying LLM call in {backoff:.1f}s", provider=provider, attempt=attempt,
                        next_attempt=attempt + 1, backoff_s=backoff, exc_type=type(e).__name__)
            time.sleep(backoff)
            continue
        duration = time.perf_counter() - start
        metrics.LLM_DURATION.labels(provider=provider).observe(duration)
        metrics.LLM_REQUESTS.labels(provider=provider, status="ok").inc()
        log.info("llm_call", msg="LLM call completed", provider=provider, duration_ms=int(duration * 1000),
                 input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, status="ok", attempt=attempt,
                 tool_calls=len(resp.tool_calls))
        metrics.LLM_TOKENS.labels(direction="input").inc(resp.input_tokens)
        metrics.LLM_TOKENS.labels(direction="output").inc(resp.output_tokens)
        metrics.LLM_COST.labels(provider=provider).inc(cost_usd(llm.model, resp.input_tokens, resp.output_tokens))
        return resp
    raise AssertionError("unreachable")


def build_llm() -> FaultInjectingLLM:
    backend = os.environ.get("FIXIT_LLM", "mock").lower()
    fault = os.environ.get("FIXIT_FAULT", "none").lower()
    if backend == "anthropic":
        inner: LLM = AnthropicLLM()
    elif backend == "ollama":
        inner = OllamaLLM()
    elif backend == "mock":
        inner = MockLLM()
    else:
        raise ValueError(f"unknown FIXIT_LLM backend {backend!r}")
    return FaultInjectingLLM(inner, mode=fault)
