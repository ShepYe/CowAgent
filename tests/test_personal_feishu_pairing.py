"""Pairing records only verified metadata until the desktop explicitly confirms."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from channel.feishu.pairing import FeishuPairing
from common.personal_tasks import TaskError


@pytest.fixture
def flow():
    pairing = FeishuPairing()
    channel = SimpleNamespace(instance_id='feishu', cfg=lambda key: 'app-test')
    state = pairing.begin('feishu', 'app-test')
    event = {
        'app_id': 'app-test', 'tenant_key': 'tenant-test',
        'sender': {'sender_type': 'user', 'tenant_key': 'tenant-test', 'sender_id': {'open_id': 'ou-owner'}},
        'message': {'chat_type': 'p2p', 'message_type': 'text', 'content': json.dumps({'text': state['command']})},
    }
    return pairing, channel, state, event


def test_capture_requires_explicit_desktop_confirmation(flow):
    pairing, channel, state, event = flow
    save = Mock()
    with pytest.raises(TaskError):
        pairing.confirm(state['pairing_id'], 'app-test', save)
    assert pairing.capture(channel, event)
    save.assert_not_called()
    assert pairing.snapshot()['state'] == 'pending'
    binding = pairing.confirm(state['pairing_id'], 'app-test', save)
    assert binding == dict(channel_instance_id='feishu', app_id='app-test',
                           tenant_id='tenant-test', sender_open_id='ou-owner')
    save.assert_called_once_with(binding)
    with pytest.raises(TaskError):
        pairing.confirm(state['pairing_id'], 'app-test', save)
    # Even after confirming, platform retries must not enter business history.
    assert pairing.capture(channel, event)
    assert pairing.snapshot() == {'state': 'confirmed'}


@pytest.mark.parametrize('case', ['group', 'bot', 'app', 'tenant', 'missing_sender', 'wrong_code', 'wrong_instance'])
def test_invalid_event_cannot_become_candidate(flow, case):
    pairing, channel, _, event = flow
    if case == 'group':
        event['message']['chat_type'] = 'group'
    elif case == 'bot':
        event['sender']['sender_type'] = 'app'
    elif case == 'app':
        event['app_id'] = 'other-app'
    elif case == 'tenant':
        event['tenant_key'] = 'other-tenant'
    elif case == 'missing_sender':
        del event['sender']['sender_id']
    elif case == 'wrong_code':
        event['message']['content'] = json.dumps({'text': '/bind COW-不正确'})
    else:
        channel.instance_id = 'another-instance'
    pairing.capture(channel, event)
    assert pairing.snapshot()['state'] == 'waiting'
    assert 'candidate' not in pairing.snapshot()


def test_other_messages_and_attachments_do_not_enter_pairing(flow):
    pairing, channel, _, event = flow
    event['message']['content'] = '{"text":"你好"}'
    assert pairing.capture(channel, event) is True
    event['message']['message_type'] = 'file'
    assert pairing.capture(channel, event) is True
    assert pairing.snapshot()['state'] == 'waiting'


@pytest.mark.parametrize('invalidate', ['expire', 'cancel', 'restart', 'new_code', 'changed_app'])
def test_old_candidate_cannot_be_confirmed(flow, monkeypatch, invalidate):
    pairing, channel, state, event = flow
    pairing.capture(channel, event)
    if invalidate == 'expire':
        monkeypatch.setattr('channel.feishu.pairing.time.monotonic', lambda: float('inf'))
    elif invalidate == 'cancel':
        pairing.cancel(state['pairing_id'])
    elif invalidate == 'restart':
        pairing = FeishuPairing()
    elif invalidate == 'new_code':
        pairing.begin('feishu', 'app-test')
    save = Mock()
    with pytest.raises(TaskError):
        pairing.confirm(state['pairing_id'], 'changed-app' if invalidate == 'changed_app' else 'app-test', save)
    save.assert_not_called()


def test_pending_candidate_cannot_be_replaced_and_parallel_confirm_saves_once(flow):
    pairing, channel, state, event = flow
    pairing.capture(channel, event)
    stranger = copy.deepcopy(event)
    stranger['sender']['sender_id']['open_id'] = 'ou-stranger'
    pairing.capture(channel, stranger)
    assert pairing.snapshot()['candidate']['sender_open_id'] == 'ou-owner'
    save = Mock()

    def confirm():
        try:
            pairing.confirm(state['pairing_id'], 'app-test', save)
            return True
        except TaskError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: confirm(), range(2))) == [False, True]
    save.assert_called_once()


def test_saving_failure_preserves_pending_candidate(flow):
    pairing, channel, state, event = flow
    pairing.capture(channel, event)
    with pytest.raises(OSError):
        pairing.confirm(state['pairing_id'], 'app-test', Mock(side_effect=OSError('fixture full disk')))
    assert pairing.snapshot()['state'] == 'pending'
    pairing.confirm(state['pairing_id'], 'app-test', Mock())


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_pairing_api_requires_desktop_authentication(method, monkeypatch):
    from channel.web.api import personal
    monkeypatch.setattr(personal, '_require_auth', lambda: None)
    monkeypatch.setattr(personal, '_desktop_token_matches', lambda: False)
    monkeypatch.setattr(personal.web.webapi.ctx, 'headers', [], raising=False)
    with pytest.raises(personal.web.HTTPError):
        getattr(personal.PersonalFeishuPairingHandler(), method)()


def test_pairing_api_starts_only_selected_channel_and_saves_observed_identity(flow, monkeypatch):
    from channel.feishu import pairing as pairing_module
    from channel.web.api import personal
    from config import conf

    pairing, channel, _, _ = flow
    channel._ws_thread = None
    channel._startup_error = '请在桌面本人飞书绑定'
    channel.report_startup_success = Mock()
    manager = SimpleNamespace(restart=Mock())
    monkeypatch.setattr(pairing_module, 'pairing', pairing)
    monkeypatch.setattr(personal, '_require_auth', lambda: None)
    monkeypatch.setattr(personal, '_desktop_token_matches', lambda: True)
    monkeypatch.setattr(personal.web, 'header', lambda *args, **kwargs: None)
    monkeypatch.setattr(personal, '_pairing_channel', lambda instance: (manager, channel))
    monkeypatch.setattr('common.personal_tasks.personal_enabled', lambda: True)
    register = Mock()
    monkeypatch.setattr('channel.feishu.personal.register', register)
    saved = Mock()
    monkeypatch.setattr('channel.web.api.integrations.save_config_values', saved)
    original = copy.deepcopy(conf().get('personal_feishu_binding'))
    body = {'action': 'start', 'channel_instance_id': 'feishu'}
    monkeypatch.setattr(personal, '_body', lambda: body)
    handler = personal.PersonalFeishuPairingHandler()
    state = json.loads(handler.POST())['pairing']
    assert state['state'] == 'waiting'
    saved.assert_not_called()
    event = {'app_id': 'app-test', 'sender': {'sender_type': 'user', 'tenant_key': 'tenant',
             'sender_id': {'open_id': 'ou-owner'}}, 'message': {'chat_type': 'p2p', 'message_type': 'text',
             'content': json.dumps({'text': state['command']})}}
    pairing.capture(channel, event)
    body = {'action': 'confirm', 'pairing_id': state['pairing_id'], 'binding': {'sender_open_id': 'ou-spoof'}}
    assert json.loads(handler.POST())['pairing']['state'] == 'confirmed'
    actual = saved.call_args.args[0]['personal_feishu_binding']
    assert actual['sender_open_id'] == 'ou-owner'
    assert actual['tenant_id'] == 'tenant'
    assert conf().get('personal_feishu_binding') == original  # Tests never alter the user's configuration.
    register.assert_called_once_with(channel)


def test_rebinding_is_metadata_only_even_for_previous_owner(flow):
    pairing, channel, _, event = flow
    event['message']['content'] = '{"text":"普通消息"}'
    assert pairing.capture(channel, event) is True
    event['message']['message_type'] = 'file'
    assert pairing.capture(channel, event) is True
    assert pairing.snapshot()['state'] == 'waiting'


def test_padded_and_replayed_pairing_commands_never_enter_business(flow):
    pairing, channel, state, event = flow
    event['message']['content'] = json.dumps({'text': state['command'] + ' ' * 1024})
    assert pairing.capture(channel, event) is True
    assert pairing.snapshot()['state'] == 'waiting'
    pairing.cancel(state['pairing_id'])
    assert pairing.capture(channel, event) is True


def test_app_mismatch_permanently_invalidates_candidate(flow):
    pairing, channel, state, event = flow
    pairing.capture(channel, event)
    channel.cfg = lambda key: 'another-app'
    assert not pairing.active_for(channel)
    channel.cfg = lambda key: 'app-test'
    with pytest.raises(TaskError):
        pairing.confirm(state['pairing_id'], 'app-test', Mock())


def test_saved_configuration_revision_invalidates_even_when_app_is_restored(flow):
    pairing, channel, _, event = flow
    version = [1]
    state = pairing.begin('feishu', 'app-test', lambda: version[0])
    event['message']['content'] = json.dumps({'text': state['command']})
    pairing.capture(channel, event)
    version[0] = 3  # A -> B -> A: values match, but the saved configuration changed.
    with pytest.raises(TaskError):
        pairing.confirm(state['pairing_id'], 'app-test', Mock())


def test_atomic_save_precondition_rechecks_configuration_before_writing(flow):
    pairing, channel, _, event = flow
    version = [1]
    state = pairing.begin('feishu', 'app-test', lambda: version[0])
    event['message']['content'] = json.dumps({'text': state['command']})
    pairing.capture(channel, event)
    written = Mock()

    def save(binding):
        version[0] = 2  # Another configuration save won the save lock.
        pairing.check_revision()
        written(binding)

    with pytest.raises(TaskError):
        pairing.confirm(state['pairing_id'], 'app-test', save)
    written.assert_not_called()


def test_real_config_a_b_a_writes_change_revision_and_reject_old_candidate(flow, tmp_path, monkeypatch):
    from channel.feishu.pairing import configuration_revision
    from channel.web.core._common import _write_config_file_for_write
    from common import settings_revision

    monkeypatch.setattr('config.get_data_root', lambda: str(tmp_path))
    monkeypatch.setattr('agent.team.team_file', lambda settings: tmp_path / 'team.json')
    config_file = tmp_path / 'config.json'
    _write_config_file_for_write(str(config_file), {'feishu_app_id': 'A'})
    pairing, channel, _, event = flow
    state = pairing.begin('feishu', 'app-test', configuration_revision)
    event['message']['content'] = json.dumps({'text': state['command']})
    pairing.capture(channel, event)
    previous_revision = settings_revision.current()
    _write_config_file_for_write(str(config_file), {'feishu_app_id': 'B'})
    _write_config_file_for_write(str(config_file), {'feishu_app_id': 'A'})
    assert settings_revision.current() == previous_revision + 2
    with pytest.raises(TaskError):
        pairing.confirm(state['pairing_id'], 'app-test', Mock())


def test_roster_write_waits_for_confirmation_revision_lock(tmp_path, monkeypatch):
    import threading
    from agent import team
    from common.settings_revision import lock, current

    monkeypatch.setattr(team, 'team_file', lambda settings: tmp_path / 'team.json')
    started = threading.Event()
    finished = threading.Event()
    before = current()

    def write():
        started.set()
        team.write({}, {'channel_instances': []})
        finished.set()

    with lock:
        thread = threading.Thread(target=write)
        thread.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
        assert not (tmp_path / 'team.json').exists()
    thread.join(2)
    assert finished.is_set()
    assert current() == before + 1


def test_pairing_view_reports_real_connection_error_without_binding_prompt(flow, monkeypatch):
    from channel.feishu import pairing as module
    from channel.web.api import personal

    pairing, channel, _, _ = flow
    monkeypatch.setattr(module, 'pairing', pairing)
    monkeypatch.setattr(personal.web, 'header', lambda *args, **kwargs: None)
    manager = SimpleNamespace(get_channel=lambda name: channel)
    monkeypatch.setattr('channel.web.core._common._live_channel_manager', lambda: manager)
    channel._startup_error = '请在桌面本人飞书绑定'
    assert 'connection_error' not in json.loads(personal.PersonalFeishuPairingHandler._view())['pairing']
    channel._startup_error = 'fixture websocket unavailable'
    view = json.loads(personal.PersonalFeishuPairingHandler._view())['pairing']
    assert view['connection_error'] == channel._startup_error
    assert view['state'] == 'waiting'


def test_no_saved_binding_is_changed_by_cancel_or_expire(flow, monkeypatch):
    from config import conf

    pairing, channel, state, event = flow
    binding = copy.deepcopy(conf().get('personal_feishu_binding'))
    pairing.capture(channel, event)
    pairing.cancel(state['pairing_id'])
    monkeypatch.setattr('channel.feishu.pairing.time.monotonic', lambda: float('inf'))
    assert pairing.capture(channel, event)  # Command is still withheld from business input.
    assert conf().get('personal_feishu_binding') == binding
