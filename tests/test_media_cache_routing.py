"""媒体请求不复用网关的文字缓存路由，后续纯文本仍可复用。"""
from copy import deepcopy

import pytest

from agent.protocol.tool_media import ToolMediaContent, ToolMediaReference, _pending
from common.runtime_identity import identity_scope
from models.openai_compatible_bot import OpenAICompatibleBot


@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('mime,data', [('application/pdf', b'%PDF-fixture'), ('image/png', b'png-fixture')])
def test_media_avoids_text_cache_route_and_followup_keeps_it(monkeypatch, stream, mime, data):
    class Bot(OpenAICompatibleBot):
        def get_api_config(self):
            return dict(api_base='https://offline.invalid/v1', api_type='responses')

    requests, states = [], []

    class Client:
        def responses(self, **kwargs):
            requests.append(deepcopy(kwargs))
            # 复现网关对带媒体的文字缓存路由提前断流。
            failed = 'prompt_cache_key' in kwargs and any(
                isinstance(item.get('output'), list) for item in kwargs['input'])
            response = dict(status='failed' if failed else 'completed', output=[])
            if failed:
                response['error'] = dict(code='upstream_stream_break')
            if kwargs['stream']:
                return iter([dict(type='response.failed' if failed else 'response.completed', response=response)])
            return response

    bot = Bot()
    monkeypatch.setattr(bot, '_get_http_client', Client)
    messages = [dict(role='assistant', tool_calls=[dict(id='call-media', type='function',
        function=dict(name='read', arguments='{}'))]),
        dict(role='tool', tool_call_id='call-media', content='reading-reference')]

    with identity_scope(user_id='local-owner', session_id='media-session'):
        token = _pending.set({'call-media': (ToolMediaReference(
            lambda context: ToolMediaContent(mime, data), lambda context: True, states.append),)})
        try:
            result = bot.call_with_tools(messages, stream=stream, model='gpt-6-test')
            if stream:
                list(result)
            assert states == ['sent', 'completed']
            assert 'prompt_cache_key' not in requests[0]
            # 字节只消费一次；同一会话的文本回执继续使用原缓存键。
            result = bot.call_with_tools(messages, stream=stream, model='gpt-6-test')
            if stream:
                list(result)
            assert requests[1]['prompt_cache_key'].startswith('cow_')
            assert states == ['sent', 'completed']
        finally:
            _pending.reset(token)
