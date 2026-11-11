# encoding:utf-8
"""Responses 的缓存和推理统计应保留到 Chat usage。所有输入均为本地数据。"""

import pytest

from agent.protocol.agent_stream import AgentStreamExecutor
from models.openai.responses_adapter import (
    responses_stream_to_chat_chunks,
    responses_to_chat_completion,
)


@pytest.fixture(params=["sync", "response.completed", "response.incomplete"])
def convert_usage(request):
    def convert(usage):
        response = {"status": "completed", "output": [], "usage": usage}
        if request.param == "sync":
            return responses_to_chat_completion(response)["usage"]
        if request.param == "response.incomplete":
            response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        chunks = list(responses_stream_to_chat_chunks(iter([
            {"type": "response.output_text.delta", "delta": "本地回答"},
            {"type": request.param, "response": response},
        ])))
        assert "usage" not in chunks[0]
        assert chunks[-1]["choices"][0]["finish_reason"] == (
            "length" if request.param == "response.incomplete" else "stop"
        )
        return chunks[-1].get("usage")

    return convert


@pytest.mark.parametrize("source,target,field", [
    ("input_tokens_details", "prompt_tokens_details", "cached_tokens"),
    ("output_tokens_details", "completion_tokens_details", "reasoning_tokens"),
])
@pytest.mark.parametrize("count", [0, 48])
def test_detail_counts_survive_conversion(convert_usage, source, target, field, count):
    usage = convert_usage({
        "input_tokens": 100,
        "output_tokens": 60,
        "total_tokens": 160,
        source: {field: count},
    })

    assert usage.get(target) == {field: count}
    assert {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")} == {
        "prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160,
    }


def test_cache_usage_reaches_agent_normalization(convert_usage):
    usage = convert_usage({
        "input_tokens": 100,
        "output_tokens": 60,
        "total_tokens": 160,
        "input_tokens_details": {"cached_tokens": 80},
        "output_tokens_details": {"reasoning_tokens": 48},
    })

    assert AgentStreamExecutor._normalize_stream_usage(usage) == {
        "prompt_tokens": 100,
        "completion_tokens": 60,
        "total_tokens": 160,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 20,
        "calls": 1,
    }


@pytest.mark.parametrize("details", [{}, {"input_tokens_details": None, "output_tokens_details": None}])
def test_old_usage_without_details_keeps_token_counters(convert_usage, details):
    assert convert_usage({
        "input_tokens": 100, "output_tokens": 60, "total_tokens": 160, **details,
    }) == {"prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160}


def test_empty_usage_keeps_zero_counters(convert_usage):
    assert convert_usage({}) == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
