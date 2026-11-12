"""Responses uses flat function definitions and flat forced-function choices."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from models.openai import responses_adapter as adapter
from models.openai_compatible_bot import OpenAICompatibleBot


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("nested", [True, False])
def test_function_strictness_is_preserved(strict, nested):
    fn = {"name": "read", "parameters": {"type": "object"}, "strict": strict}
    tool = {"type": "function", "function": fn} if nested else {"type": "function", **fn}
    original = deepcopy(tool)
    converted = adapter.tools_to_responses([tool])[0]
    assert converted["strict"] is strict
    assert tool == original


def test_forced_function_choice_is_flattened_without_mutating_input():
    choice = {"type": "function", "function": {"name": "read"}}
    payload = adapter.build_responses_payload(model="test", messages=[], tools=[{"name": "read"}], tool_choice=choice)
    assert payload["tool_choice"] == {"type": "function", "name": "read"}
    assert choice == {"type": "function", "function": {"name": "read"}}


@pytest.mark.parametrize('nested', [False, True])
def test_optional_tool_parameters_keep_best_effort_semantics(nested):
    schema = {'type': 'object', 'properties': {'path': {'type': 'string'},
              'offset': {'type': 'integer'}}, 'required': ['path']}
    fn = {'name': 'read', 'parameters': schema}
    tool = {'type': 'function', 'function': fn} if nested else fn
    converted = adapter.tools_to_responses([tool])[0]
    assert converted['strict'] is False
    assert converted['parameters']['required'] == ['path']


@pytest.mark.parametrize("choice", ["auto", "required", "none", {"type": "function", "name": "read"}])
def test_existing_responses_choices_are_preserved(choice):
    payload = adapter.build_responses_payload(model="test", messages=[], tools=[{"name": "read"}], tool_choice=choice)
    assert payload["tool_choice"] == choice


class Bot(OpenAICompatibleBot):
    def get_api_config(self):
        return {"model": "test", "api_type": "responses", "api_base": "https://test.invalid/v1"}


def test_native_bot_http_request_preserves_function_contract(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"status": "completed", "output": []}
    request = Mock(return_value=response)
    monkeypatch.setattr("models.openai.openai_http_client.requests.post", request)
    Bot().call_with_tools(
        [{"role": "user", "content": "read"}],
        tools=[{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}, "strict": False}}],
        tool_choice={"type": "function", "function": {"name": "read"}},
    )
    payload = request.call_args.kwargs["json"]
    assert payload["tool_choice"] == {"type": "function", "name": "read"}
    assert payload["tools"][0]["strict"] is False


def collect_calls(events):
    calls = {}
    for chunk in adapter.responses_stream_to_chat_chunks(events, 'test'):
        for choice in chunk.get('choices', []):
            for delta in choice.get('delta', {}).get('tool_calls', []):
                call = calls.setdefault(delta['index'], {'id': '', 'name': '', 'arguments': ''})
                call['id'] += delta.get('id') or ''
                call['name'] += delta.get('function', {}).get('name') or ''
                call['arguments'] += delta.get('function', {}).get('arguments') or ''
    return calls


@pytest.mark.parametrize('stage', ['added', 'done', 'completed'])
def test_snapshot_only_tool_arguments_reach_agent(stage):
    item = dict(type='function_call', call_id='call-1', name='memory_search', arguments='{"query":"校准"}')
    event = (dict(type='response.completed', response=dict(status='completed', output=[item]))
             if stage == 'completed' else dict(type=f'response.output_item.{stage}', output_index=0, item=item))
    assert collect_calls([event])[0] == {'id': 'call-1', 'name': 'memory_search', 'arguments': item['arguments']}


def test_interleaved_deltas_and_snapshots_do_not_repeat_parameters_or_ids():
    first = dict(type='function_call', call_id='a', name='read', arguments='{"path":"a"}')
    second = dict(type='function_call', call_id='b', name='read', arguments='{"path":"b"}')
    events = [dict(type='response.output_item.added', output_index=i, item=dict(item, arguments=''))
              for i, item in ((1, first), (2, second))]
    events += [dict(type='response.function_call_arguments.delta', output_index=2, delta='{"path":'),
               dict(type='response.function_call_arguments.delta', output_index=1, delta=first['arguments']),
               dict(type='response.function_call_arguments.done', output_index=2, arguments=second['arguments']),
               dict(type='response.output_item.done', output_index=1, item=first),
               dict(type='response.completed', response=dict(status='completed', output=[dict(type='reasoning'), first, second]))]
    assert collect_calls(events) == {0: {'id': 'a', 'name': 'read', 'arguments': first['arguments']},
                                     1: {'id': 'b', 'name': 'read', 'arguments': second['arguments']}}


def test_conflicting_argument_snapshot_fails_before_tool_execution():
    item = dict(type='function_call', call_id='a', name='read', arguments='{"path":"a"}')
    events = [dict(type='response.output_item.added', output_index=0, item=item),
              dict(type='response.output_item.done', output_index=0, item=dict(item, arguments='{"path":"other"}'))]
    with pytest.raises(ValueError, match='^invalid_function_call_stream$'):
        collect_calls(events)
