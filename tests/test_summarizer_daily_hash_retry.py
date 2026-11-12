"""日终去重必须在候选提交完成后生效；失败、无模型、处理中分别保留语义。"""
import threading

import pytest

from agent.memory.ledger import MemoryLedger
from agent.memory.summarizer import MemoryFlushManager
from test_summarizer_flush_retry import Model, window, pending


def test_a_day_whose_flush_never_landed_stays_retryable(tmp_path):
    model = Model(failures=100)
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    assert manager.create_daily_summary(messages)
    assert manager.create_daily_summary(messages)
    assert model.calls == 2
    assert manager._last_flushed_content_hash == ""
    assert not manager._daily_inflight_hashes
    assert not pending(tmp_path)


def test_the_day_is_recovered_once_the_provider_returns(tmp_path):
    model = Model(failures=1)
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    assert manager.create_daily_summary(messages)
    assert not pending(tmp_path)
    assert manager.create_daily_summary(messages)
    assert len(pending(tmp_path)) == 1
    assert not manager.create_daily_summary(messages)
    assert model.calls == 2


def test_a_day_that_landed_is_still_deduplicated(tmp_path):
    model = Model()
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    assert manager.create_daily_summary(messages)
    assert not manager.create_daily_summary(messages)
    assert model.calls == 1
    assert manager._last_flushed_content_hash


def test_an_inflight_day_is_not_submitted_twice(tmp_path):
    entered, release = threading.Event(), threading.Event()
    model = Model(entered=entered, release=release)
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(manager.create_daily_summary(messages)))
    worker.start()
    try:
        assert entered.wait(3)
        assert not manager.create_daily_summary(messages)
    finally:
        release.set()
        worker.join(3)
    assert outcomes == [True]
    assert model.calls == 1
    assert len(pending(tmp_path)) == 1


def test_a_model_that_records_nothing_still_closes_the_day(tmp_path):
    manager = MemoryFlushManager(tmp_path, Model(empty=True))
    messages = window()
    assert manager.create_daily_summary(messages)
    assert not manager.create_daily_summary(messages)
    assert not pending(tmp_path)
    assert manager._last_flushed_content_hash


def test_a_failed_write_hands_the_day_back(tmp_path, monkeypatch):
    manager = MemoryFlushManager(tmp_path, Model())
    real = MemoryLedger.record
    monkeypatch.setattr(MemoryLedger, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    messages = window()
    assert not manager.create_daily_summary(messages)
    assert manager._last_flushed_content_hash == ""
    assert not manager._daily_inflight_hashes
    monkeypatch.setattr(MemoryLedger, "record", real)
    assert manager.create_daily_summary(messages)
    assert len(pending(tmp_path)) == 1


def test_new_content_on_the_same_day_is_a_new_day(tmp_path):
    manager = MemoryFlushManager(tmp_path, Model())
    assert manager.create_daily_summary(window())
    assert manager.create_daily_summary([{"role": "user", "content": "move metrics into own table"}])
    assert len(pending(tmp_path)) == 2


@pytest.mark.parametrize("reason", ["trim", "overflow"])
def test_a_trim_flush_does_not_touch_the_day_ledger(tmp_path, reason):
    manager = MemoryFlushManager(tmp_path, Model())
    assert manager.flush_from_messages(window(), reason=reason)
    assert not manager._daily_flushed_hashes
    assert not manager._daily_inflight_hashes
    assert manager._last_flushed_content_hash == ""
