"""摘要失败仍可重试；成功/处理中按原话身份防重，候选先于裁剪保存。"""
import json
import threading

import pytest

from agent.memory.ledger import MemoryLedger
from agent.memory.summarizer import MemoryFlushManager


SUMMARY = "ingest split and schema stays frozen"


def window():
    return [{"role": "assistant", "content": "Split ingest into a separate process; freeze schema until Q3."}]


class Model:
    def __init__(self, failures=0, entered=None, release=None, empty=False):
        self.calls = 0
        self.failures = failures
        self.entered = entered
        self.release = release
        self.empty = empty

    def call(self, request):
        self.calls += 1
        if self.entered:
            self.entered.set()
            assert self.release.wait(3), "test did not release the model"
        if self.calls <= self.failures:
            raise ConnectionError("provider is unreachable")
        data = json.loads(request.messages[0]["content"].split("\n", 1)[1])
        ref = {k: data["evidence"][0][k] for k in ("message_id", "block_index", "start", "end", "sha256")}
        candidate = dict(content=SUMMARY, disposition="undecided", reason="needs main decision", basis=[ref])
        value = dict(context_summary=SUMMARY, entries=[] if self.empty else [candidate])
        return {"content": [{"type": "text", "text": json.dumps(value)}]}


def pending(tmp_path):
    return MemoryLedger(tmp_path).pending_candidates()


def test_a_flush_that_wrote_nothing_can_be_retried(tmp_path):
    model = Model(failures=100)
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    callbacks = []
    assert manager.flush_from_messages(messages, context_summary_callback=callbacks.append)
    assert manager.flush_from_messages(messages, context_summary_callback=callbacks.append)
    assert model.calls == 2  # fallback never spends the model retry eligibility
    assert len(callbacks) == 2
    assert not pending(tmp_path)
    assert not list(tmp_path.glob("memory/????-??-??.md"))


def test_the_retry_recovers_the_window_once_the_provider_returns(tmp_path):
    model = Model(failures=1)
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    assert manager.flush_from_messages(messages)
    assert not pending(tmp_path)
    assert manager.flush_from_messages(messages)
    assert model.calls == 2
    assert pending(tmp_path)[0]["candidate"]["content"] == SUMMARY
    assert not MemoryLedger(tmp_path).active_entries()  # assistant summary is not human evidence


def test_a_flush_still_in_flight_is_not_submitted_twice(tmp_path):
    entered, release = threading.Event(), threading.Event()
    model = Model(entered=entered, release=release)
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(manager.flush_from_messages(messages)))
    worker.start()
    try:
        assert entered.wait(3)
        assert manager.flush_from_messages(messages) is False
    finally:
        release.set()
        worker.join(3)
    assert outcomes == [True]
    assert model.calls == 1
    assert len(pending(tmp_path)) == 1


def test_a_written_flush_is_deduplicated(tmp_path):
    model = Model()
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    assert manager.flush_from_messages(messages)
    assert manager.flush_from_messages(messages)  # already saved: trimming remains permitted
    assert model.calls == 1
    assert len(pending(tmp_path)) == 1


def test_a_failed_write_can_be_retried(tmp_path, monkeypatch):
    manager = MemoryFlushManager(tmp_path, Model())
    original = MemoryLedger.record
    attempts = []
    def flaky_record(self, *args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("disk full")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MemoryLedger, "record", flaky_record)
    messages = window()
    assert not manager.flush_from_messages(messages)
    assert not pending(tmp_path)
    assert manager.flush_from_messages(messages)
    assert len(pending(tmp_path)) == 1
    assert len(attempts) == 2


@pytest.mark.parametrize("empty", [True, False])
def test_a_successful_result_is_not_asked_again(tmp_path, empty):
    model = Model(empty=empty)
    manager = MemoryFlushManager(tmp_path, model)
    messages = window()
    assert manager.flush_from_messages(messages)
    assert manager.flush_from_messages(messages)
    assert model.calls == 1
