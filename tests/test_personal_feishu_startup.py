"""An enabled personal Feishu channel must expose why it did not connect."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def channel(monkeypatch):
    from channel.feishu import feishu_channel, personal

    config = {
        'personal_assistant_enabled': True, 'feishu_app_id': 'app-test',
        'feishu_app_secret': 'fixture-secret', 'feishu_event_mode': 'websocket',
        'channel_type': 'feishu',
    }
    monkeypatch.setattr(feishu_channel, '_ensure_lark_imported', lambda: None)
    monkeypatch.setattr(feishu_channel, 'conf', lambda: config)
    monkeypatch.setattr(personal, 'conf', lambda: config)
    monkeypatch.setattr('common.personal_tasks.personal_enabled', lambda: True)
    result = feishu_channel.FeiShuChanel.new_instance()
    monkeypatch.setattr(result, 'cfg', config.get)
    monkeypatch.setattr(result, '_fetch_bot_open_id', Mock())
    monkeypatch.setattr(result, '_startup_websocket', Mock())
    return result, config


def test_unbound_start_logs_action_and_makes_no_remote_request(channel, monkeypatch):
    from channel.feishu import feishu_channel

    ch, _ = channel
    warning = Mock()
    monkeypatch.setattr(feishu_channel.logger, 'warning', warning)
    ch.startup()
    ok, error = ch.wait_startup(0)
    assert not ok
    assert '本人飞书' in error
    ch._fetch_bot_open_id.assert_not_called()
    ch._startup_websocket.assert_not_called()
    assert any('本人飞书' in str(call) for call in warning.call_args_list)


def test_enabled_card_surfaces_blocked_start_instead_of_claiming_connected(channel, monkeypatch):
    from channel.web.api import channels

    ch, config = channel
    ch.startup()
    monkeypatch.setattr(channels, 'conf', lambda: config)
    monkeypatch.setattr(channels, '_require_auth', lambda: None)
    monkeypatch.setattr(channels.web, 'header', lambda *args, **kwargs: None)
    monkeypatch.setattr(channels.web, 'input', lambda: {})
    monkeypatch.setattr(channels.ChannelsHandler, '_multi_agent_mode', staticmethod(lambda: False))
    monkeypatch.setattr(channels, '_live_channel_manager', lambda: SimpleNamespace(get_channel=lambda name: ch))
    cards = json.loads(channels.ChannelsHandler().GET())['channels']
    card = next(card for card in cards if card['name'] == 'feishu')
    assert card['active'] is True  # Enabled configuration is retained for reconnect.
    assert '本人飞书' in card.get('startup_error', '')
    assert card['personal_binding_required'] is True


def test_binding_only_connection_consumes_code_without_task_or_model(channel, monkeypatch):
    from channel.feishu import pairing as module
    from channel.feishu.pairing import FeishuPairing

    ch, _ = channel
    pairing = FeishuPairing()
    monkeypatch.setattr(module, 'pairing', pairing)
    state = pairing.begin('feishu', 'app-test')
    ch.startup()
    ch._startup_websocket.assert_called_once()
    event = dict(app_id='app-test', sender={'sender_type': 'user', 'tenant_key': 'tenant',
                 'sender_id': {'open_id': 'ou-owner'}}, message={'chat_type': 'p2p', 'message_type': 'text',
                 'content': json.dumps({'text': state['command']})})
    monkeypatch.setattr('channel.feishu.personal.receive', Mock(side_effect=AssertionError('business dispatch')))
    ch._handle_message_event(event)
    assert pairing.snapshot()['state'] == 'pending'


def test_unbound_other_messages_are_rejected_before_storage(channel, monkeypatch):
    ch, _ = channel
    monkeypatch.setattr('channel.feishu.personal.TaskStore', Mock(side_effect=AssertionError('task storage')))
    ch._handle_message_event({'message': {'chat_type': 'p2p', 'message_type': 'file', 'content': '{}'}})


def test_starting_after_binding_clears_previous_block(channel, monkeypatch):
    ch, config = channel
    ch.startup()
    assert ch._personal_binding_required
    config['personal_feishu_binding'] = dict(channel_instance_id='feishu', app_id='app-test',
                                            tenant_id='tenant', sender_open_id='ou-owner')
    monkeypatch.setattr('channel.feishu.personal.register', Mock())
    ch.startup()
    assert not ch._personal_binding_required
    assert ch._startup_error is None
    ch._startup_websocket.assert_called_once()
