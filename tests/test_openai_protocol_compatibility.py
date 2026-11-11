# encoding:utf-8
"""协议默认值与已证实的型号限制分离；所有请求使用本地客户端。"""

import pytest

from models.openai import responses_adapter


@pytest.mark.parametrize("model,expected", [
    ("gpt-6-astra", True), ("gpt-6.2-sol", True), ("gpt-7-sol", True),
    ("gpt-60", True), (" GPT-7.1-SOL ", True),
    ("gpt-5.5", False), ("gpt-6alias", False), ("my-reasoner", False),
])
def test_auto_routing_uses_numeric_gpt_major_version(model, expected):
    assert responses_adapter.use_responses_api(model) is expected
    assert responses_adapter.use_responses_api(model, "chat") is False
    assert responses_adapter.use_responses_api(model, "responses") is True


@pytest.mark.parametrize("model,expected", [
    ("gpt-6-astra", True), ("gpt-6-astra-2026-10-01", True),
    ("gpt-6.1-sol", True), (" GPT-6.1-SOL-2026-11-01 ", True),
    ("gpt-6.1-sol-preview", False), ("gpt-6-astra-fast", False),
    ("gpt-6.1-terra", False), ("gpt-6.2-sol", False), ("gpt-7-sol", False),
    ("gpt-60", False), ("my-reasoner", False), (None, False),
])
def test_thinking_only_helper_covers_only_confirmed_models_and_snapshots(model, expected):
    assert responses_adapter.is_thinking_only_model(model) is expected


@pytest.mark.parametrize("model", ["gpt-6.1-terra", "gpt-6.2-sol", "gpt-7-sol", "gpt-60", "my-reasoner"])
@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_responses_payload_preserves_unconfirmed_model_effort(model, effort):
    payload = responses_adapter.build_responses_payload(
        model=model, messages=[{"role": "user", "content": "你好"}], reasoning_effort=effort,
    )
    assert payload["reasoning"] == {"effort": effort}


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol-2026-11-01"])
@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_responses_payload_clamps_confirmed_thinking_only_models(model, effort):
    payload = responses_adapter.build_responses_payload(model=model, messages=[], reasoning_effort=effort)
    assert payload["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("model", ["gpt-6.2-sol", "gpt-7-sol", "my-reasoner"])
def test_unconfirmed_model_effort_is_an_opaque_provider_value(model):
    payload = responses_adapter.build_responses_payload(
        model=model, messages=[], reasoning_effort="VendorFutureTier",
    )
    assert payload["reasoning"] == {"effort": "VendorFutureTier"}
