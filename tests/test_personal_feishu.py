from types import SimpleNamespace

import pytest

from channel.feishu.personal import authorize, receive
from common.personal_tasks import TaskError


@pytest.fixture
def identity(monkeypatch):
    binding = dict(channel_instance_id="feishu", app_id="app-1", tenant_id="tenant-1", sender_open_id="ou-owner")
    monkeypatch.setattr("channel.feishu.personal.conf", lambda: {"personal_feishu_binding": binding})
    ch = SimpleNamespace(instance_id="feishu", cfg=lambda key: "app-1", _startup_ts=0)
    event = dict(sender={"sender_id": {"open_id": "ou-owner"}, "tenant_key": "tenant-1"},
                 message={"chat_type": "p2p", "message_id": "msg-1", "content": '{"text":"hello"}', "message_type": "text"})
    return ch, event


def test_owner_and_tenant_are_both_required(identity):
    ch, event = identity
    assert authorize(ch, event)["sender_open_id"] == "ou-owner"
    event["sender"]["tenant_key"] = "another-tenant"
    with pytest.raises(TaskError, match="forbidden"):
        authorize(ch, event)


def test_stranger_rejected_before_download_model_and_task_storage(identity, monkeypatch):
    ch, event = identity
    event["sender"]["sender_id"]["open_id"] = "ou-stranger"
    monkeypatch.setattr("channel.feishu.personal.TaskStore", lambda: pytest.fail("禁止先建立任务库"))
    with pytest.raises(TaskError, match="forbidden"):
        receive(ch, event)


def test_group_and_unbound_instance_are_rejected(identity):
    ch, event = identity
    event["message"]["chat_type"] = "group"
    with pytest.raises(TaskError, match="forbidden"):
        authorize(ch, event)
    ch.instance_id = "other"
    with pytest.raises(TaskError, match="请在本机"):
        authorize(ch, event)
