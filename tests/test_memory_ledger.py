"""条目准入、断点投影、更正、并发及旧数据的真实 SQLite 回归。"""
import copy
import hashlib
import json
import sqlite3
import threading

import pytest

from agent.memory.conversation_store import ConversationStore
from agent.memory.ledger import MemoryError, MemoryLedger
from common.runtime_identity import identity_scope


@pytest.fixture
def ledger(tmp_path):
    with identity_scope(agent_id="", user_id="owner", session_id="chat"):
        store = ConversationStore(tmp_path / "memory" / "long-term" / "index.db")
        yield MemoryLedger(tmp_path, store)


def fact(ledger, text="以后回答简洁些", start=0, end=None, disposition="conversation_memory"):
    message = {"role": "user", "content": text}
    ledger.capture([message], origin="human")
    evidence = ledger.evidence([message])[0]
    evidence.pop("text")
    evidence.pop("origin")
    evidence.update(start=start, end=len(text) if end is None else end)
    return dict(content=text[start:evidence["end"]], disposition=disposition, basis=[evidence], reason="用户真实表达")


def test_source_session_independent_preference_and_mixed_fragments(ledger):
    from agent.knowledge.purpose import note_source_evidence
    note_source_evidence()
    text = "以后多用表格，这次测试预算是42 dB，不要记"
    preference = fact(ledger, text, end=6)
    transient = dict(preference, content="测试预算42 dB", disposition="archive_only",
                     basis=[dict(preference["basis"][0], start=7, end=len(text))])
    receipt = ledger.record("mixed", [preference, transient], trusted=True, expected_revision=0)
    assert [r["status"] for r in receipt["results"]] == ["remembered", "archive_only"]
    assert ledger.active_entries()[0]["content"] == text[:6]
    assert "42" not in ledger.filter_text(ledger._projection_path(), ledger._projection_path().read_text(encoding="utf-8"))


def test_candidate_reused_across_summary_and_restart(ledger):
    proposal = fact(ledger)
    first = ledger.record("active", [proposal], trusted=True)["results"][0]
    rewrite = dict(proposal, content="用户希望简洁回答")
    restarted = MemoryLedger(ledger.workspace, ledger.store)
    second = restarted.record("summary", [rewrite])["results"][0]
    assert first["candidate_id"] == second["candidate_id"]
    assert second["status"] == "undecided"  # 改写歧义须主 Agent 沿原候选确认。
    assert len(restarted.active_entries()) == 1
    assert restarted.workspace_id == ledger.workspace_id


def test_pending_summary_does_not_self_approve(ledger):
    proposal = fact(ledger)
    pending = ledger.record("background", [proposal])["results"][0]
    assert pending["status"] == "undecided"
    assert not ledger.active_entries()
    assert ledger.record("main", [dict(proposal, candidate_id=pending["candidate_id"])], trusted=True)["results"][0]["status"] == "remembered"


def test_reject_forged_range_owner_and_internal_user(ledger):
    proposal = fact(ledger, "我😀喜欢简洁")
    invalid = dict(proposal, basis=[dict(proposal["basis"][0], end=999)])
    assert ledger.record("range", [invalid], trusted=True)["results"][0]["error"] == "invalid_evidence"
    invalid = dict(proposal, owner="someone")
    assert ledger.record("owner", [invalid], trusted=True)["results"][0]["error"] == "invalid_evidence"
    with identity_scope(user_id="another"):
        other = MemoryLedger(ledger.workspace, ledger.store)
        assert other.record("cross", [proposal], trusted=True)["results"][0]["error"] == "invalid_evidence"
    message = {"role": "user", "content": "压缩摘要：用户喜欢长回答"}
    ledger.capture([message])
    reference = ledger.evidence([message])[0]
    basis = {k: reference[k] for k in ("message_id", "block_index", "start", "end", "sha256")}
    assert ledger.record("replay", [dict(proposal, basis=[basis])], trusted=True)["results"][0]["error"] == "invalid_evidence"


def test_test_same_words_does_not_consume_real_preference(ledger):
    proposal = fact(ledger)
    # 真人确实说过也不绕过显式任务用途。
    with ledger._transaction() as db:
        row = db.execute("SELECT record FROM memory_evidence WHERE message_id=?", (proposal["basis"][0]["message_id"],)).fetchone()
        archived = json.loads(row[0])
        archived["extras"]["archive_only"] = True
        db.execute("UPDATE memory_evidence SET record=? WHERE message_id=?", (json.dumps(archived), proposal["basis"][0]["message_id"]))
    assert ledger.record("bad", [proposal], trusted=True)["results"][0]["error"] == "purpose_conflict"
    real = fact(ledger)
    assert ledger.record("real", [real], trusted=True)["results"][0]["status"] == "remembered"


def test_projection_failure_retry_idempotency_and_partial_receipts(ledger, monkeypatch):
    proposal = fact(ledger)
    project = ledger.project
    monkeypatch.setattr(ledger, "project", lambda: False)
    receipt = ledger.record("retry", [proposal], trusted=True)
    assert receipt["results"][0]["status"] == "pending_projection"
    assert not receipt["results"][0]["searchable"]
    monkeypatch.setattr(ledger, "project", project)
    assert ledger.record("retry", [copy.deepcopy(proposal)], trusted=True)["results"][0]["status"] == "remembered"
    assert len(ledger.active_entries()) == 1
    assert ledger.record("retry", [dict(proposal, content="别的")], trusted=True)["results"][0]["error"] == "idempotency_conflict"
    assert ledger.record("partial", [dict(proposal, basis=[]), proposal], trusted=True)["results"][0]["status"] == "failed"


def test_correction_filters_stale_files_and_invalidates_dream(ledger):
    old = ledger.record("old", [fact(ledger, "用户喜欢长回答")], trusted=True)["results"][0]
    derived = ledger.merge("dream", "用户偏好长回答", [old["entry_id"]], ledger.revision())["results"][0]
    stale = ledger._projection_path().read_text(encoding="utf-8")
    revision = ledger.revision()
    new = fact(ledger, "以后回答简洁些")
    new["supersedes"] = [old["entry_id"]]
    ledger.record("correction", [new], trusted=True, expected_revision=revision)
    current = ledger.active_entries()
    assert len(current) == 1 and current[0]["content"] == new["content"]
    assert derived["entry_id"] not in [e["entry_id"] for e in current]
    assert ledger.filter_text(ledger._projection_path(), stale) == ""
    assert ledger.record("old", [fact_copy(ledger, old["candidate_id"])], trusted=True)["results"][0]["error"] == "idempotency_conflict"
    assert ledger.merge("late", "旧结论", [old["entry_id"]], revision)["results"][0]["status"] == "failed"


def fact_copy(ledger, cid):
    with ledger._transaction() as db:
        candidate = json.loads(db.execute("SELECT record FROM memory_candidates WHERE candidate_id=?", (cid,)).fetchone()[0])
    return {k: candidate[k] for k in ("content", "disposition", "basis", "reason")}


def test_concurrent_workers_do_not_lose_entries_and_stale_revision_blocks(ledger):
    one, two = fact(ledger, "偏好一"), fact(ledger, "偏好二")
    failures = []
    def worker(request, proposal):
        try:
            with identity_scope(user_id="owner", session_id="chat"):
                other = MemoryLedger(ledger.workspace, ledger.store)
                assert other.record(request, [proposal], trusted=True)["results"][0]["status"] in ("remembered", "pending_projection")
        except BaseException as exc:
            failures.append(exc)
    threads = [threading.Thread(target=worker, args=("one", one)), threading.Thread(target=worker, args=("two", two))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert len(ledger.active_entries()) == 2
    stale = ledger.record("late", [fact(ledger, "旧worker")], trusted=True, expected_revision=0)
    assert stale["results"][0]["error"] == "version_conflict"


def test_external_changes_unknown_schema_and_legacy_correction_preserved(ledger):
    ledger.record("first", [fact(ledger)], trusted=True)
    path = ledger._projection_path()
    path.write_text("外部更改", encoding="utf-8")
    assert ledger.project() is False
    assert path.read_text(encoding="utf-8") == "外部更改"
    legacy = ledger.workspace / "MEMORY.md"
    legacy.write_text("旧偏好：长回答\n其他信息", encoding="utf-8")
    proposal = fact(ledger)
    proposal["legacy_supersedes"] = [dict(path="MEMORY.md", sha256=hashlib.sha256(legacy.read_bytes()).hexdigest(), start=0, end=8)]
    receipt = ledger.record("legacy", [proposal], trusted=True)
    assert receipt["results"][0]["status"] != "failed", receipt
    assert "长回答" not in ledger.filter_text(legacy, legacy.read_text(encoding="utf-8"))
    assert "长回答" in legacy.read_text(encoding="utf-8")
    with sqlite3.connect(ledger.db_path) as db:
        db.execute("UPDATE memory_schema SET version=999")
    with pytest.raises(MemoryError, match="未知记忆格式"):
        MemoryLedger(ledger.workspace, ledger.store)


def test_human_edit_uses_new_basis_and_rejects_stale_baseline(ledger):
    ledger.record("old", [fact(ledger)], trusted=True)
    path = ledger._projection_path()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = ledger.human_edit(path, "用户希望详细回答", expected_hash=digest)
    entry = ledger.active_entries()[0]
    assert entry["entry_id"] == result["entry_id"]
    assert entry["basis"][0]["type"] == "human_edit"
    with pytest.raises(MemoryError):
        ledger.human_edit(path, "迟到修改", expected_hash=digest)


def test_main_purpose_change_and_late_summary_cannot_resurrect(ledger):
    proposal = fact(ledger)
    first = ledger.record("first", [proposal], trusted=True)["results"][0]
    archive = dict(proposal, candidate_id=first["candidate_id"], disposition="archive_only")
    receipt = ledger.record("exclude", [archive], trusted=True)
    assert receipt["results"][0]["status"] == "archive_only"
    assert not ledger.active_entries()
    late = ledger.record("late", [proposal])["results"][0]
    assert late["status"] == "failed" and late["error"] == "purpose_conflict"
    assert ledger.record("relabel", [proposal], trusted=True)["results"][0]["error"] == "purpose_conflict"


def test_human_commit_survives_projection_failure(ledger, monkeypatch):
    path = ledger.workspace / "MEMORY.md"
    path.write_text("旧内容", encoding="utf-8")
    replacement = ledger._strict_replace
    monkeypatch.setattr(ledger, "_strict_replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    receipt = ledger.human_edit(path, "用户显式新内容")
    assert receipt["status"] == "pending_projection"
    assert ledger.active_entries()[0]["content"] == "用户显式新内容"
    assert path.read_text(encoding="utf-8") == "旧内容"
    monkeypatch.setattr(ledger, "_strict_replace", replacement)
    assert ledger.project()
    assert path.read_text(encoding="utf-8") == "用户显式新内容"


def test_read_and_memory_service_filter_excluded_projection(ledger):
    from agent.memory.service import MemoryService
    from agent.tools.read.read import Read
    first = ledger.record("read", [fact(ledger)], trusted=True)["results"][0]
    path = ledger._projection_path()
    result = Read(dict(cwd=str(ledger.workspace), workspace_root=str(ledger.workspace))).execute(dict(path=str(path)))
    assert "回答简洁" in str(result.result) and "cow-entry" not in str(result.result)
    ledger.exclude(first["entry_id"])
    result = MemoryService(str(ledger.workspace)).get_content(path.name)
    assert "回答简洁" not in result["content"]


def test_first_capture_cannot_promote_internal_or_steal_user(ledger):
    from agent.memory.message_trace import ensure_trace
    message = dict(role="user", content="工具重放的偏好")
    ensure_trace(message, user_id="owner", session_id="chat", origin="internal")
    ledger.store.append_messages("chat", [message])
    forged = copy.deepcopy(message)
    forged["extras"]["memory_trace"]["origin"] = "human"
    with pytest.raises(MemoryError):
        ledger.capture([forged])
    message = dict(role="user", content="Alice的真实偏好")
    ensure_trace(message, user_id="alice", session_id="chat", origin="human")
    ledger.store.append_messages("chat", [message])
    with pytest.raises(MemoryError):
        ledger.capture([message])


def test_replacement_before_projection_receipt_recovers(ledger, monkeypatch):
    proposal = fact(ledger)
    original = ledger._strict_replace
    def crash(*args):
        original(*args)
        raise OSError("文件已替换，回执尚未提交")
    monkeypatch.setattr(ledger, "_strict_replace", crash)
    first = ledger.record("crash", [proposal], trusted=True)
    assert first["results"][0]["status"] == "pending_projection"
    restarted = MemoryLedger(ledger.workspace, ledger.store)
    assert restarted.record("crash", [proposal], trusted=True)["results"][0]["status"] == "remembered"


def test_human_edit_merge_invalidates_all_inputs(ledger):
    one = ledger.record("long", [fact(ledger, "喜欢长回答")], trusted=True)["results"][0]
    ledger.merge("dream", "用户喜欢详细长回答", [one["entry_id"]], ledger.revision())
    ledger.human_edit(ledger._projection_path(), "以后只要简洁回答")
    assert [e["content"] for e in ledger.active_entries()] == ["以后只要简洁回答"]


def test_ambiguous_candidate_persists_then_explicit_rewrite(ledger):
    proposal = fact(ledger)
    first = ledger.record("first", [proposal], trusted=True)["results"][0]
    rewritten = dict(proposal, content="用户偏好简洁回答")
    assert ledger.record("ambiguity", [rewritten], trusted=True)["results"][0]["status"] == "undecided"
    assert ledger.pending_candidates()[0]["candidate"]["content"] == rewritten["content"]
    rewritten["candidate_id"] = first["candidate_id"]
    receipt = ledger.record("confirm", [rewritten], trusted=True)
    assert receipt["results"][0]["status"] == "remembered"
    assert [e["content"] for e in ledger.active_entries()] == [rewritten["content"]]


def test_extras_transient_and_unknown_schema_reject_all_writes(ledger):
    message = dict(role="user", content="临时测试偏好", extras=dict(purpose="transient"))
    ledger.capture([message], origin="human")
    reference = ledger.evidence([message])[0]
    basis = {k: reference[k] for k in ("message_id", "block_index", "start", "end", "sha256")}
    proposal = dict(content="临时测试偏好", disposition="conversation_memory", reason="test", basis=[basis])
    assert ledger.record("transient", [proposal], trusted=True)["results"][0]["error"] == "purpose_conflict"
    first = ledger.record("valid", [fact(ledger)], trusted=True)["results"][0]
    with sqlite3.connect(ledger.db_path) as db:
        db.execute("UPDATE memory_schema SET version=999")
    with pytest.raises(MemoryError):
        ledger.exclude(first["entry_id"])
    with sqlite3.connect(ledger.db_path) as db:
        assert db.execute("SELECT state FROM memory_entries WHERE entry_id=?", (first["entry_id"],)).fetchone()[0] == "active"


def test_partial_failed_evidence_can_retry_without_duplicate_success(ledger):
    one, two = fact(ledger, "第一偏好"), fact(ledger, "第二偏好")
    mid = two["basis"][0]["message_id"]
    with ledger._transaction() as db:
        saved = db.execute("SELECT record FROM memory_evidence WHERE message_id=?", (mid,)).fetchone()[0]
        db.execute("DELETE FROM memory_evidence WHERE message_id=?", (mid,))
    first = ledger.record("partial-retry", [one, two], trusted=True)
    assert [r["status"] for r in first["results"]] == ["remembered", "failed"]
    with ledger._transaction() as db:
        db.execute("INSERT INTO memory_evidence VALUES (?,?,?,?,?)", (mid, *ledger.scope, ledger.session_id, saved))
    retry = ledger.record("partial-retry", [one, two], trusted=True)
    assert [r["status"] for r in retry["results"]] == ["remembered", "remembered"]
    assert retry["results"][0]["entry_id"] == first["results"][0]["entry_id"]
    assert len(ledger.active_entries()) == 2


def test_source_metadata_cannot_be_removed_on_first_capture(ledger):
    from agent.memory.message_trace import ensure_trace
    message = dict(role="user", content="只是临时偏好", extras=dict(purpose="transient", archive_only=True))
    ensure_trace(message, user_id="owner", session_id="chat", origin="human")
    ledger.store.append_messages("chat", [message])
    claimed = copy.deepcopy(message)
    claimed["extras"] = dict(memory_trace=message["extras"]["memory_trace"])
    ledger.capture([claimed])
    ref = ledger.evidence([claimed])[0]
    proposal = dict(content="临时偏好", disposition="conversation_memory", reason="test",
                    basis=[{key: ref[key] for key in ("message_id", "block_index", "start", "end", "sha256")}])
    assert ledger.record("remove-purpose", [proposal], trusted=True)["results"][0]["error"] == "purpose_conflict"


def test_interrupted_projection_then_new_revision_recovers(ledger, monkeypatch):
    original = ledger._strict_replace
    def interrupted(*args):
        original(*args)
        raise OSError("after replace")
    monkeypatch.setattr(ledger, "_strict_replace", interrupted)
    ledger.record("first-interrupted", [fact(ledger, "第一偏好")], trusted=True)
    monkeypatch.setattr(ledger, "_strict_replace", original)
    receipt = ledger.record("second-revision", [fact(ledger, "第二偏好")], trusted=True)
    assert receipt["results"][0]["status"] == "remembered"
    text = ledger.filter_text(ledger._projection_path(), ledger._projection_path().read_text(encoding="utf-8"))
    assert "第一偏好" in text and "第二偏好" in text


def test_human_edit_damaged_projection_still_supersedes_authoritative_inputs(ledger):
    ledger.record("one", [fact(ledger, "旧偏好一")], trusted=True)
    ledger.record("two", [fact(ledger, "旧偏好二")], trusted=True)
    path = ledger._projection_path()
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("<!-- cow-entry", "<!-- broken-entry", 1), encoding="utf-8")
    ledger.human_edit(path, "用户显式的新偏好")
    assert [entry["content"] for entry in ledger.active_entries()] == ["用户显式的新偏好"]


@pytest.mark.parametrize("derived", [False, True])
def test_correction_rejects_dependency_on_replaced_fact_before_any_mutation(ledger, derived):
    old = ledger.record("old-long", [fact(ledger, "以后详细回答")], trusted=True)["results"][0]
    dependency = ledger.active_entries()[0]
    if derived:
        merged = ledger.merge("derived-long", "用户希望详细回答", [old["entry_id"]], ledger.revision())["results"][0]
        dependency = next(entry for entry in ledger.active_entries() if entry["entry_id"] == merged["entry_id"])
    before = ledger.active_entries()
    revision = ledger.revision()
    new = fact(ledger, "改成简洁回答")
    new["supersedes"] = [old["entry_id"]]
    invalid = dict(new, basis=new["basis"] + [dict(entry_id=dependency["entry_id"], revision=dependency["revision"])])
    receipt = ledger.record("invalid-correction", [invalid], trusted=True)
    assert receipt["results"][0]["status"] == "failed"
    assert receipt["results"][0]["error"] == "purpose_conflict"
    assert ledger.active_entries() == before
    assert ledger.revision() == revision
    assert ledger.record("valid-correction", [new], trusted=True)["results"][0]["status"] == "remembered"
    assert [entry["content"] for entry in ledger.active_entries()] == [new["content"]]


@pytest.mark.parametrize("failure", ["missing", "permission"])
def test_bad_legacy_reference_fails_only_its_batch_item(ledger, monkeypatch, failure):
    from pathlib import Path
    good = fact(ledger, "正常偏好")
    legacy = ledger.workspace / "missing.md"
    bad = fact(ledger, "另一偏好")
    bad["legacy_supersedes"] = [dict(path=legacy.name, sha256="0" * 64, start=0, end=1)]
    if failure == "permission":
        read_bytes = Path.read_bytes
        def denied(path):
            if path == legacy:
                raise PermissionError("denied")
            return read_bytes(path)
        monkeypatch.setattr(Path, "read_bytes", denied)
    receipt = ledger.record("legacy-partial", [good, bad], trusted=True)
    assert [item["status"] for item in receipt["results"]] == ["remembered", "failed"]
    assert receipt["results"][1]["error"] == ("invalid_evidence" if failure == "missing" else "storage_failed")
    assert [entry["content"] for entry in ledger.active_entries()] == [good["content"]]


@pytest.mark.parametrize("restricted", ["archive_only", "sourced_knowledge"])
def test_restricted_span_cannot_be_relabelled_by_shorter_reference(ledger, restricted):
    proposal = fact(ledger, "测试台词：我喜欢长回答", disposition=restricted)
    ledger.record("restrict-span", [proposal], trusted=True)
    shorter = dict(proposal, content="喜欢长回答", disposition="conversation_memory",
                   basis=[dict(proposal["basis"][0], start=5)])
    result = ledger.record("shorter-span", [shorter], trusted=True)["results"][0]
    assert result["status"] == "failed" and result["error"] == "purpose_conflict"
    assert not ledger.active_entries()
    assert ledger.record("new-real", [fact(ledger, "我喜欢长回答")], trusted=True)["results"][0]["status"] == "remembered"


def test_restricting_subspan_invalidates_wider_old_fact_and_derivation(ledger):
    proposal = fact(ledger, "这句只是测试：我喜欢长回答")
    old = ledger.record("wide-fact", [proposal], trusted=True)["results"][0]
    ledger.merge("wide-dream", "用户喜欢长回答", [old["entry_id"]], ledger.revision())
    restrict = dict(proposal, content="测试片段不保留", disposition="archive_only",
                    basis=[dict(proposal["basis"][0], start=7)])
    assert ledger.record("exclude-subspan", [restrict], trusted=True)["results"][0]["status"] == "archive_only"
    assert not ledger.active_entries()


def test_pending_new_projection_prevents_stale_editor_from_excluding_unseen_fact(ledger, monkeypatch):
    ledger.record("visible-a", [fact(ledger, "可见的偏好A")], trusted=True)
    path = ledger._projection_path()
    old_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    project = ledger.project
    monkeypatch.setattr(ledger, "project", lambda: False)
    pending = ledger.record("hidden-b", [fact(ledger, "尚未显示的偏好B")], trusted=True)
    assert pending["results"][0]["status"] == "pending_projection"
    before = ledger.active_entries()
    revision = ledger.revision()
    with pytest.raises(MemoryError) as conflict:
        ledger.human_edit(path, "编辑旧A", expected_hash=old_hash)
    assert conflict.value.code == "version_conflict"
    assert ledger.active_entries() == before
    assert ledger.revision() == revision
    monkeypatch.setattr(ledger, "project", project)
    assert ledger.project()
    current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert ledger.human_edit(path, "编辑最新A与B", expected_hash=current_hash)["status"] == "remembered"


def test_pending_generation_plan_also_prevents_stale_editor_save(ledger, monkeypatch):
    ledger.record("visible", [fact(ledger, "可见偏好")], trusted=True)
    path = ledger._projection_path()
    baseline = hashlib.sha256(path.read_bytes()).hexdigest()
    def fail(*args):
        raise OSError("projection failed before replace")
    monkeypatch.setattr(ledger, "_strict_replace", fail)
    pending = ledger.record("unseen", [fact(ledger, "投影失败的新偏好")], trusted=True)
    assert pending["results"][0]["status"] == "pending_projection"
    with pytest.raises(MemoryError) as conflict:
        ledger.human_edit(path, "旧文件编辑", expected_hash=baseline)
    assert conflict.value.code == "version_conflict"
    assert len(ledger.active_entries()) == 2
