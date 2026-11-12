"""Dream 只合并已准入条目；投影失败保留旧文件，权威接受与可读投影分别核验。"""
import json
import os

import pytest

from agent.memory.ledger import MemoryLedger
from agent.memory.summarizer import MemoryFlushManager


OLD_MEMORY = "- legacy memory stays readable\n"


class DreamModel:
    def __init__(self, before_return=None, malformed=False):
        self.requests = []
        self.before_return = before_return
        self.malformed = malformed

    def call(self, request):
        self.requests.append(request)
        active = json.loads(request.messages[0]["content"])
        assert len(active) == 2
        assert all(e["state"] == "active" for e in active)
        assert OLD_MEMORY not in request.messages[0]["content"]
        if self.before_return:
            self.before_return()
        output = "no merge structure" if self.malformed else json.dumps({"merges": [{
            "content": "concise replies using tables", "input_ids": [e["entry_id"] for e in active]}]})
        return {"content": [{"type": "text", "text": output}]}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "MEMORY.md").write_text(OLD_MEMORY, encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "2026-11-11.md").write_text("old diary unverified", encoding="utf-8")
    return tmp_path


def accept(workspace):
    ledger = MemoryLedger(workspace)
    for i, text in enumerate(["concise replies", "use tables"]):
        message = {"role": "user", "content": text}
        ledger.capture([message], origin="human")
        evidence = ledger.evidence([message])[0]
        ref = {k: evidence[k] for k in ("message_id", "block_index", "start", "end", "sha256")}
        receipt = ledger.record(f"fact-{i}", [dict(content=text, disposition="conversation_memory",
                                basis=[ref], reason="user preference")], trusted=True)
        assert receipt["results"][0]["status"] == "remembered"
    return ledger


class TruncatingHandle:
    def __init__(self, handle):
        self.handle = handle

    def write(self, text):
        self.handle.write(text[:max(len(text) // 2, 1)])
        raise OSError(28, "No space left on device")

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def __getattr__(self, key):
        return getattr(self.handle, key)


def test_an_interrupted_distillation_keeps_the_existing_memory(workspace, monkeypatch):
    ledger = accept(workspace)
    path = workspace / "memory" / "routed.md"
    original = path.read_bytes()
    real = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: TruncatingHandle(real(*a, **k)))
    manager = MemoryFlushManager(workspace, DreamModel())
    assert manager.deep_dream(force=True)  # 权威已接受；投影待完成不是回滚。
    assert path.read_bytes() == original
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == OLD_MEMORY
    assert not list(workspace.glob("memory/.cow-memory-*"))
    active = ledger.active_entries()
    assert len(active) == 1 and len(active[0]["inputs"]) == 2
    # 旧投影不冒充新投影。恢复写入后可按台账重建。
    monkeypatch.setattr(os, "fdopen", real)
    assert ledger.project()
    assert "concise replies using tables" in ledger.filter_text(path, path.read_text(encoding="utf-8"))


def test_dream_diary_is_never_written_without_ledger_basis(workspace):
    diary = workspace / "memory" / "dreams" / "2026-11-12.md"
    diary.parent.mkdir()
    diary.write_text("old dream", encoding="utf-8")
    manager = MemoryFlushManager(workspace, DreamModel())
    assert not manager._write_dream_diary("new unsupported dream")
    assert diary.read_text(encoding="utf-8") == "old dream"


def test_successful_distillation_preserves_input_identities_and_skips_repeat(workspace):
    ledger = accept(workspace)
    originals = ledger.active_entries()
    model = DreamModel()
    manager = MemoryFlushManager(workspace, model)
    assert manager.deep_dream(force=True)
    active = ledger.active_entries()
    assert len(active) == 1
    assert set(active[0]["inputs"]) == {e["entry_id"] for e in originals}
    for entry in originals:
        assert all(op in active[0]["operation_ids"] for op in entry["operation_ids"])
        assert set(entry["candidate_ids"]) <= set(active[0]["candidate_ids"])
    assert not manager.deep_dream(force=True)
    assert len(model.requests) == 1
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == OLD_MEMORY
    assert not (workspace / "memory" / "dreams").exists()


def test_legacy_only_has_no_eligible_dream_inputs(workspace):
    model = DreamModel()
    assert not MemoryFlushManager(workspace, model).deep_dream(force=True)
    assert model.requests == []


def test_malformed_reply_leaves_active_entries_alone(workspace):
    ledger = accept(workspace)
    before = ledger.active_entries()
    assert not MemoryFlushManager(workspace, DreamModel(malformed=True)).deep_dream(force=True)
    assert ledger.active_entries() == before


def test_correction_wins_before_dream_merge(workspace):
    ledger = accept(workspace)
    excluded = ledger.active_entries()[0]["entry_id"]
    model = DreamModel(before_return=lambda: ledger.exclude(excluded))
    assert not MemoryFlushManager(workspace, model).deep_dream(force=True)
    assert all(excluded not in e.get("inputs", []) for e in ledger.active_entries())
