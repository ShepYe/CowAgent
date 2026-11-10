"""Personal archives survive automatic cleanup; explicit deletion still works."""
import sqlite3

import pytest

from agent.memory.conversation_store import ConversationStore


@pytest.mark.parametrize("personal", [True, False])
def test_automatic_cleanup_respects_personal_archive(tmp_path, monkeypatch, personal):
    monkeypatch.setattr("common.personal_tasks.personal_enabled", lambda: personal)
    path = tmp_path / "history.sqlite3"
    store = ConversationStore(path)
    messages = []
    for number in range(3):
        messages.extend([
            {"role": "user", "content": f"[SCHEDULED] run {number}"},
            {"role": "assistant", "content": f"result {number}"},
        ])
    store.append_messages("scheduled", messages, channel_type="feishu")
    store.append_messages("old", [{"role": "user", "content": "complete original history"}], channel_type="feishu")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sessions SET last_active=1 WHERE session_id='old'")
    assert store.prune_scheduled_messages("scheduled", keep_last_n=1) == (0 if personal else 4)
    assert store.cleanup_old_sessions(max_age_days=1) == (0 if personal else 1)
    assert len(store.load_messages("scheduled")) == (6 if personal else 2)
    assert bool(store.load_messages("old")) == personal
    if personal:
        # These are explicit user actions, independent of the retention gates.
        store.clear_session("old")
        assert not store.load_messages("old")


def test_disabled_cleanup_does_not_open_database(tmp_path, monkeypatch):
    monkeypatch.setattr("common.personal_tasks.personal_enabled", lambda: True)
    store = ConversationStore(tmp_path / "history.sqlite3")
    monkeypatch.setattr(store, "_connect", lambda: pytest.fail("自动清理应在数据库副作用之前返回"))
    assert store.cleanup_old_sessions() == 0
    assert store.prune_scheduled_messages("any", keep_last_n=0) == 0
