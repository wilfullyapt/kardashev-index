"""xAI client: request shapes, usage/cost parsing, citations, retries, JSON extraction."""
import json

import httpx
import pytest

from app.pipeline.llm import LLMError, XAIClient, estimate_cost, extract_json

CHAT = {"model": "grok-4.3-0910", "choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "completion_tokens_details": {"reasoning_tokens": 120},
                  "prompt_tokens_details": {"cached_tokens": 1000}, "cost_in_usd_ticks": 37_500_000}}
RESP = {"model": "grok-4.3-0910", "status": "completed", "output": [
    {"type": "web_search_call", "id": "ws1"}, {"type": "web_search_call", "id": "ws2"},
    {"type": "message", "content": [{"type": "output_text", "text": "{\"sources\": []}", "annotations": [
        {"type": "url_citation", "url": "https://a.example/r", "start_index": 0, "end_index": 3},
        {"type": "url_citation", "url": "https://a.example/r"}]}]}],
    "citations": ["https://b.example/s"],
    "usage": {"input_tokens": 30000, "output_tokens": 900, "output_tokens_details": {"reasoning_tokens": 400},
              "num_server_side_tools_used": 2, "cost_in_usd_ticks": 500_000_000}}


def client_with(handler, **kw):
    return XAIClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def test_chat_request_and_parsing():
    seen = {}

    def handler(req):
        seen["path"], seen["body"], seen["auth"] = req.url.path, json.loads(req.content), req.headers["authorization"]
        return httpx.Response(200, json=CHAT)
    res = client_with(handler).chat(system="s", user="u", model="grok-4.3", max_tokens=100)
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "grok-4.3" and seen["body"]["messages"][0]["role"] == "system"
    assert res.model == "grok-4.3-0910" and res.input_tokens == 1200 and res.reasoning_tokens == 120
    assert res.cached_tokens == 1000 and res.cost_usd == pytest.approx(0.00375) and not res.cost_estimated


def test_search_uses_responses_api_with_web_search_and_collects_citations():
    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json=RESP)
    res = client_with(handler).search(system="s", user="u", model="grok-4.3", max_tool_calls=4)
    assert seen["path"] == "/v1/responses"
    assert seen["body"]["tools"] == [{"type": "web_search"}] and seen["body"]["max_tool_calls"] == 4
    assert res.text == '{"sources": []}' and res.tool_calls == 2 and res.cost_usd == pytest.approx(0.05)
    assert res.citations == ["https://a.example/r", "https://b.example/s"]


def test_search_retries_without_max_tool_calls_if_rejected():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "max_tool_calls" in body:
            return httpx.Response(400, json={"error": "unknown field max_tool_calls"})
        return httpx.Response(200, json=RESP)
    res = client_with(handler).search(system="s", user="u", model="m", max_tool_calls=4)
    assert len(bodies) == 2 and "max_tool_calls" not in bodies[1] and res.tool_calls == 2


def test_retries_transient_errors_but_not_client_errors():
    calls = []

    def flaky(req):
        calls.append(1)
        return httpx.Response(503, text="busy") if len(calls) == 1 else httpx.Response(200, json=CHAT)
    assert client_with(flaky, max_retries=1).chat(system="s", user="u", model="m").model == "grok-4.3-0910"
    assert len(calls) == 2

    def bad(req):
        calls.append(1)
        return httpx.Response(401, text="no")
    calls.clear()
    with pytest.raises(LLMError) as e:
        client_with(bad, max_retries=3).chat(system="s", user="u", model="m")
    assert e.value.status == 401 and len(calls) == 1

    def slow(req):
        raise httpx.ReadTimeout("t", request=req)
    with pytest.raises(LLMError) as e:
        client_with(slow, max_retries=1, chat_timeout_s=5).chat(system="s", user="u", model="m")
    assert e.value.retryable and "timeout" in str(e.value)


def test_cost_estimate_when_api_omits_ticks():
    data = {**CHAT, "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}}
    res = client_with(lambda r: httpx.Response(200, json=data)).chat(system="s", user="u", model="m")
    assert res.cost_estimated and res.cost_usd == pytest.approx(1.25 + 2.50)
    assert estimate_cost("grok-4.7", 1_000_000, 0) == pytest.approx(2.0)   # flagship rate, not the grok-4 alias
    assert estimate_cost("mystery", 0, 0, tool_calls=10) == pytest.approx(0.05)


def test_extract_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json('{"a": ')
    with pytest.raises(ValueError):
        extract_json("no json here")
