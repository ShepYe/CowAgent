"""生产阅读台账：隔离资料根、真实 TaskStore/解析/Wiki 守卫。"""
import copy
import json
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfReader

from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.reading import ReadingService
from agent.knowledge.reading_materials import prepare_pdf_materials, PdfBudget
from agent.knowledge.wiki import WikiService
from common.personal_tasks import TaskStore, OWNER, TaskError


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("不得出网"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "request", {"text": "整理图形"}, {"channel": "test"}, "config-1")
    output = BytesIO()
    Image.new("RGB", (80, 80), "blue").save(output, "PDF", save_all=True,
                                               append_images=[Image.new("RGB", (80, 80), "red")])
    path = tmp_path / "source.pdf"
    path.write_bytes(output.getvalue())
    assert not PdfReader(path).pages[0].extract_text()
    store.attach(OWNER, task["task_id"], path)
    task = store.claim(OWNER, task["task_id"])
    source = SourceService(workspace)
    receipt = source.receive(path, filename=path.name, request_id="receipt", origin=task["origin"])
    source.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="测试")
    base = source.parse(receipt["revision_id"], prepare_only=True)
    materials = prepare_pdf_materials(workspace, ".personal_sources/originals/" + receipt["sha256"],
        document_id=receipt["document_id"], revision_id=receipt["revision_id"], parse_id=base["parse_id"],
        config_revision=task["config_revision"], budget=PdfBudget(max_pages=1))
    registry = ReadingService(workspace, task_store=store)
    record = registry.prepare(task, receipt, base, materials)
    return registry, store, task, receipt, base, materials, record


def observation(record, index=0, key="figure"):
    segment = record["segments"][index]
    return dict(reading_id=record["reading_id"], evidence_key=key, segment_id=segment["segment_id"],
                location=dict(kind="pdf", physical_page=segment["target_pages"][0]),
                observation="图中显示蓝色方块", type="visual_interpretation")


def response():
    return dict(id="resp-real", status="completed", usage={"input_tokens": 10, "output_tokens": 5},
                output=[dict(type="message", role="assistant", content=[dict(type="output_text", text="蓝色方块")])])


def complete(bundle, index=0, save=True):
    registry, _, task, _, _, _, record = bundle
    segment = record["segments"][index]["segment_id"]
    registry.mark_sent(task, record["reading_id"], segment)
    if save:
        registry.save_response(task, record["reading_id"], segment, response())
    registry.mark_completed(task, record["reading_id"], segment)


def retry(bundle):
    _, store, task, *_ = bundle
    store.fail(OWNER, task["task_id"], task["attempt"], "interrupted", "parse")
    store.retry(OWNER, task["task_id"], task["attempt"])
    return store.claim(OWNER, task["task_id"])


def publish(registry, refs):
    wiki = WikiService(registry.workspace)
    batch = wiki.prepare([dict(title="图形主题", summary="说明蓝色图形。", category="测试",
                              blocks=[dict(content="蓝色方块", sources=list(refs.values()))])])
    return wiki, wiki.publish(batch["batch_id"])[0]


def test_prepare_types_persist_immutable_append_and_wiki_readback(bundle):
    registry, _, task, receipt, base, materials, record = bundle
    assert registry.prepare(task, receipt, base["parse_id"], materials.to_dict()) == record
    assert registry.for_task(task) == [record]
    with registry.source.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert base["units"] == []
    base_path = registry.source.root / "parses" / (base["parse_id"] + ".json")
    before = base_path.read_bytes()
    complete(bundle)
    refs = registry.resolve_evidence(task, "s", [observation(record)])
    wiki, published = publish(registry, refs)
    readback = wiki.read_reference(published["publish_id"])
    assert readback["type"] == "visual_interpretation"
    assert readback["origin"] == "model"
    assert readback["location"] == dict(kind="pdf", physical_page=1, type="visual_interpretation")
    assert readback["reading_copy"]["path"] == materials.segments[0].material_relpath
    assert readback["text"] == "图中显示蓝色方块"
    assert base_path.read_bytes() == before
    appended = registry.resolve_evidence(task, "s2", [observation(record, key="next")])
    assert next(iter(appended.values()))["ordinal"] == 1
    assert wiki.read_reference(published["publish_id"])["text"] == "图中显示蓝色方块"


@pytest.mark.parametrize("state", ["prepared", "sent", "unknown"])
def test_undelivered_unknown_no_observation_writes(bundle, state):
    registry, _, task, *_, record = bundle
    if state != "prepared":
        registry.mark_sent(task, record["reading_id"], record["segments"][0]["segment_id"])
    if state == "unknown":
        registry.mark_unknown(task, record["reading_id"], record["segments"][0]["segment_id"])
    with pytest.raises(SourceError) as error:
        registry.resolve_evidence(task, "s", [observation(record)])
    assert error.value.code == "reading_incomplete"
    with registry.source.transaction() as db:
        assert db.execute("SELECT count(*) FROM reading_aliases").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("type", "text"), ("observation", "已读取该文件"),
                                          ("segment_id", "unrelated"), ("sha256", "forged")])
def test_invalid_model_payload(bundle, field, value):
    registry, _, task, *_, record = bundle
    complete(bundle)
    item = observation(record)
    item[field] = value
    with pytest.raises(SourceError):
        registry.resolve_evidence(task, "s", [item])


def test_scope_and_page_range(bundle):
    registry, store, task, *_, record = bundle
    complete(bundle)
    item = observation(record)
    item["location"]["physical_page"] = 2
    with pytest.raises(SourceError):
        registry.resolve_evidence(task, "s", [item])
    other = store.submit(OWNER, "other", {}, {"channel": "test"}, task["config_revision"])
    other = store.claim(OWNER, other["task_id"])
    with pytest.raises(SourceError):
        registry.get(other, record["reading_id"])
    store.cancel(OWNER, task["task_id"], task["attempt"])
    with pytest.raises(TaskError):
        registry.resolve_evidence(task, "s", [observation(record)])


def test_full_payload_reorder_conflict_concurrent_restart(bundle):
    registry, store, task, _, _, _, record = bundle
    complete(bundle)
    items = [observation(record, key="a"), observation(record, key="b")]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: registry.resolve_evidence(task, "s", items), range(3)))
    assert results[0] == results[1] == results[2]
    reopened = ReadingService(registry.workspace, task_store=store)
    assert reopened.resolve_evidence(task, "s", [dict(reversed(list(e.items()))) for e in reversed(items)]) == results[0]
    changed = copy.deepcopy(items)
    changed[0]["observation"] = "不同观察"
    before = list((registry.source.root / "parses").glob("*.json"))
    with pytest.raises(SourceError) as error:
        reopened.resolve_evidence(task, "s", changed)
    assert error.value.code == "idempotency_conflict"
    assert before == list((registry.source.root / "parses").glob("*.json"))
    with pytest.raises(SourceError):
        registry.resolve_evidence(task, "duplicates", [items[0], items[0]])


def test_observation_checkpoint_binds_complete_submission_payload(bundle):
    registry, store, task, *_, record = bundle
    complete(bundle)
    evidence = [observation(record)]
    refs = registry.resolve_evidence(task, "bound-pages", evidence, submission_payload="pages-fingerprint-a")
    reopened = ReadingService(registry.workspace, task_store=store)
    assert reopened.resolve_evidence(task, "bound-pages", evidence, submission_payload="pages-fingerprint-a") == refs
    before = list((registry.source.root / "parses").glob("*.json"))
    for fingerprint in ("pages-fingerprint-b", None):
        with pytest.raises(SourceError) as error:
            reopened.resolve_evidence(task, "bound-pages", evidence, submission_payload=fingerprint)
        assert error.value.code == "idempotency_conflict"
    with pytest.raises(SourceError) as error:
        reopened.resolve_evidence(task, "bound-pages", [], submission_payload="pages-fingerprint-b")
    assert error.value.code == "idempotency_conflict"
    assert before == list((registry.source.root / "parses").glob("*.json"))


def test_empty_evidence_still_binds_submission_fingerprint(bundle):
    registry, _, task, *_ = bundle
    assert registry.resolve_evidence(task, "text-only", [], submission_payload="pages-a") == {}
    assert registry.resolve_evidence(task, "text-only", [], submission_payload="pages-a") == {}
    with pytest.raises(SourceError) as error:
        registry.resolve_evidence(task, "text-only", [], submission_payload="pages-b")
    assert error.value.code == "idempotency_conflict"
    assert registry.resolve_evidence(task, "standalone-empty", []) == {}
    with registry.source.transaction() as db:
        assert db.execute("SELECT count(*) FROM reading_aliases").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM reading_submissions").fetchone()[0] == 2


def test_multiple_readings_same_alias_and_scope(bundle):
    registry, _, task, receipt, base, materials, first = bundle
    complete(bundle)
    # 显式不同范围的材料形成不同 reading，可各用 figure 别名。
    one_page = prepare_pdf_materials(registry.workspace, materials.original_relpath,
        document_id=receipt["document_id"], revision_id=receipt["revision_id"], parse_id=base["parse_id"],
        config_revision=task["config_revision"], target_pages=[2])
    second = registry.prepare(task, receipt, base, one_page)
    sid = second["segments"][0]["segment_id"]
    registry.mark_sent(task, second["reading_id"], sid)
    registry.mark_completed(task, second["reading_id"], sid, "resp-2")
    refs = registry.resolve_evidence(task, "s", [observation(first), observation(second)])
    assert len(refs) == 2
    assert set(refs) == {(first["reading_id"], "figure"), (second["reading_id"], "figure")}


def test_resume_all_keeps_producing_identity_rejects_late_callbacks(bundle):
    registry, _, task, *_, record = bundle
    complete(bundle)
    refs = registry.resolve_evidence(task, "s", [observation(record)])
    newer = retry(bundle)
    with pytest.raises(SourceError):
        registry.get(newer, record["reading_id"])
    resumed = registry.resume_all(newer)[0]
    assert resumed["attempt"] == 1 and resumed["segments"][0]["saved_model_output"] == response()["output"]
    assert registry.resolve_evidence(newer, "s", [observation(record)]) == refs
    with pytest.raises(TaskError):
        registry.mark_completed(task, record["reading_id"], record["segments"][0]["segment_id"])
    with pytest.raises(SourceError):
        registry.mark_sent(newer, record["reading_id"], record["segments"][1]["segment_id"])


def test_prepare_auto_resumes_fully_completed_prior_attempt(bundle):
    registry, _, _, receipt, base, materials, record = bundle
    complete(bundle, 0)
    complete(bundle, 1)
    newer = retry(bundle)
    reused = registry.prepare(newer, receipt, base, materials)
    assert reused["reading_id"] == record["reading_id"] and reused["attempt"] == 1
    assert registry.get(newer, reused["reading_id"]) == reused


def test_observation_saved_then_read_uses_fixed_native_parse_anchor(bundle, monkeypatch):
    from agent.knowledge.reading_flow import prepare_pdf_read
    registry, store, task, receipt, base, _, _ = bundle
    store._change(OWNER, task["task_id"], task["attempt"], {"running"},
        source_native_parses={receipt["revision_id"]: base["parse_id"]})
    monkeypatch.setattr("agent.knowledge.reading_flow.prepare_task_target", lambda store, task, root, config=None: task)
    monkeypatch.setattr("agent.knowledge.reading_capabilities.capability_identity", lambda *a: None)
    args = (store, task, registry.workspace, receipt, object(), 1, 1)
    _, parsed, materials, service, _ = prepare_pdf_read(*args)
    first = service.prepare(task, receipt, parsed, materials)
    sid = first["segments"][0]["segment_id"]
    service.mark_sent(task, first["reading_id"], sid)
    service.save_response(task, first["reading_id"], sid, response())
    service.mark_completed(task, first["reading_id"], sid)
    service.resolve_evidence(task, "observation-before-read", [observation(first)])
    _, parsed2, materials2, service2, _ = prepare_pdf_read(*args)
    second = service2.prepare(task, receipt, parsed2, materials2)
    assert parsed2["parse_id"] == parsed["parse_id"] == base["parse_id"]
    assert second["reading_id"] == first["reading_id"]
    assert second["segments"][0]["saved_model_output"] == response()["output"]


def test_saved_response_before_delivery_callback_can_recover(bundle):
    registry, _, task, *_, record = bundle
    sid = record["segments"][0]["segment_id"]
    registry.mark_sent(task, record["reading_id"], sid)
    registry.save_response(task, record["reading_id"], sid, response())
    newer = retry(bundle)
    resumed = registry.resume(newer, record["reading_id"])
    assert resumed["segments"][0]["state"] == "completed"
    assert resumed["segments"][0]["saved_model_output"] == response()["output"]
    assert registry.resolve_evidence(newer, "s", [observation(record)])


def test_same_attempt_prepare_recovers_saved_response_before_final_callback(bundle):
    registry, _, task, receipt, base, materials, record = bundle
    sid = record["segments"][0]["segment_id"]
    registry.mark_sent(task, record["reading_id"], sid)
    registry.save_response(task, record["reading_id"], sid, response())
    recovered = registry.prepare(task, receipt, base, materials)
    assert recovered["reading_id"] == record["reading_id"]
    assert recovered["segments"][0]["state"] == "completed"
    assert recovered["segments"][0]["saved_model_output"] == response()["output"]


def test_observation_write_failure_rolls_back_aliases_and_retries_stably(bundle, monkeypatch):
    import agent.knowledge.reading as module
    registry, _, task, *_, record = bundle
    complete(bundle)
    original = module.write_text_atomic
    calls = []

    def interrupt_second(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 2:
            raise OSError("夹具存储中断")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "write_text_atomic", interrupt_second)
    evidence = [observation(record, key="a"), dict(observation(record, key="b"), type="ocr")]
    with pytest.raises(OSError):
        registry.resolve_evidence(task, "s", evidence)
    with registry.source.transaction() as db:
        assert db.execute("SELECT count(*) FROM reading_aliases").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM reading_submissions").fetchone()[0] == 0
    monkeypatch.setattr(module, "write_text_atomic", original)
    refs = registry.resolve_evidence(task, "s", list(reversed(evidence)))
    assert sorted(r["ordinal"] for r in refs.values()) == [0, 1]
    assert registry.resolve_evidence(task, "s", evidence) == refs
    # 新版本保留旧单元，旧观察可由新 parse 原 ordinal 回查。
    latest = max(refs.values(), key=lambda r: r["ordinal"])
    old_in_new = dict(latest, ordinal=0)
    wiki, pub = publish(registry, {("latest", "old"): old_in_new})
    assert wiki.read_reference(pub["publish_id"])["type"] == "visual_interpretation"


@pytest.mark.parametrize("target", ["original", "copy", "base", "source_state", "response"])
def test_tamper_and_withdrawal_rejected(bundle, target):
    registry, _, task, receipt, base, materials, record = bundle
    complete(bundle)
    if target == "source_state":
        registry.source.change(receipt["document_id"], action="withdraw", expected_state=1, reason="撤回")
    elif target == "response":
        with registry.source.transaction() as db:
            row = json.loads(db.execute("SELECT record FROM reading_records WHERE id=?", (record["reading_id"],)).fetchone()[0])
            row["segments"][0]["saved_model_output"] = []
            db.execute("UPDATE reading_records SET record=? WHERE id=?", (json.dumps(row), record["reading_id"]))
    else:
        path = (registry.workspace / materials.original_relpath if target == "original" else
                registry.workspace / materials.segments[0].material_relpath if target == "copy" else
                registry.source.root / "parses" / (base["parse_id"] + ".json"))
        path.write_bytes(b"tampered")
    with pytest.raises((SourceError, ValueError)):
        registry.resolve_evidence(task, "s", [observation(record)])


def test_different_root_restore_source_and_copy_readback(bundle, tmp_path):
    from cli.commands.backup import create_backup_archive, restore_backup_archive
    registry, store, task, *_, record = bundle
    complete(bundle)
    refs = registry.resolve_evidence(task, "s", [observation(record)])
    wiki, published = publish(registry, refs)
    (tmp_path / "config.json").write_text(json.dumps(dict(personal_assistant_enabled=True,
        agent_workspace=str(registry.workspace))), encoding="utf-8")
    archive = tmp_path.parent / (tmp_path.name + "-reading-backup.zip")
    create_backup_archive(archive, tmp_path, registry.workspace, complete=True)
    restored_data = tmp_path.parent / (tmp_path.name + "-restored")
    restored_root = restored_data / "workspace"
    restore_backup_archive(archive, restored_data, restored_root)
    shutil.rmtree(registry.workspace)
    restored = ReadingService(restored_root, task_store=store)
    assert restored.resolve_evidence(task, "s", [observation(record)]) == refs
    result = WikiService(restored_root).read_reference(published["publish_id"])
    assert result["original"].startswith(str(restored_root))
    assert (restored_root / result["reading_copy"]["path"]).exists()


def test_unknown_record_version_blocks_registry_writes(bundle):
    registry, store, task, *_, record = bundle
    with registry.source.transaction() as db:
        altered = dict(record, schema_version=999)
        db.execute("UPDATE reading_records SET record=? WHERE id=?", (json.dumps(altered), record["reading_id"]))
    with pytest.raises(SourceError):
        ReadingService(registry.workspace, task_store=store)


def test_media_context_capability_and_response_checkpoint(bundle, monkeypatch):
    from common.runtime_identity import RuntimeIdentity
    from agent.knowledge.reading_capabilities import _key
    registry, _, task, *_, record = bundle
    sid = record["segments"][0]["segment_id"]
    identity = RuntimeIdentity(user_id=OWNER, session_id="session")
    provider = dict(model="model", provider="provider", endpoint_hash=__import__("hashlib").sha256(b"https://test").hexdigest(),
                    protocol="responses", mime_type="application/pdf", transport="inline", config_revision=task["config_revision"])
    context = dict(protocol="responses", model="model", api_base="https://test", identity=identity,
                   task_id=task["task_id"], config_revision=task["config_revision"],
                   provider="provider", mime_type="application/pdf", transport="inline")
    monkeypatch.setattr("models.config_revision.require_config_revision", lambda revision: None)
    ref = registry.media_reference(task, record["reading_id"], sid, identity=identity, provider_identity=provider)
    assert ref.supports(context) is False
    with registry.source.transaction() as db:
        db.execute("CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)")
        db.execute("INSERT INTO reading_capabilities VALUES(?,?)", (_key(provider), json.dumps(dict(state="supported", identity=provider))))
    assert ref.supports(context) is True
    assert ref.supports(dict(context, provider="other-provider")) is False
    assert ref.supports(dict(context, transport="url")) is False
    assert ref.supports(dict(context, identity=RuntimeIdentity(user_id=OWNER, session_id="other"))) is False
    assert ref.loader(context).data.startswith(b"%PDF-")
    ref.on_delivery("sent")
    ref.on_response(response())
    ref.on_delivery("completed")
    saved = registry.get(task, record["reading_id"])["segments"][0]
    assert saved["response_id"] == "resp-real" and saved["full_response_saved"]
    assert saved["response_checkpoint"] == "response_saved"


def test_response_input_and_base64_forbidden(bundle):
    registry, _, task, *_, record = bundle
    sid = record["segments"][0]["segment_id"]
    registry.mark_sent(task, record["reading_id"], sid)
    for payload in (dict(response(), input=[]), dict(response(), output=[dict(type="message", content="data:application/pdf;base64,AAAA")])):
        with pytest.raises(SourceError):
            registry.save_response(task, record["reading_id"], sid, payload)


def new_task(bundle, revision=None):
    _, store, task, *_ = bundle
    created = store.submit(OWNER, "reuse-" + __import__("uuid").uuid4().hex, {}, {"channel": "test"},
                           revision or task["config_revision"])
    return store.claim(OWNER, created["task_id"])


def test_cross_task_cache_new_identity_preserves_original_output_and_model(bundle):
    registry, _, task, receipt, base, materials, original = bundle
    output = response()
    output["output"] = [dict(type="function_call", name="knowledge_ingest", call_id="original-call",
                            arguments=json.dumps(dict(reading_id=original["reading_id"], task_id=task["task_id"])))]
    for segment in original["segments"]:
        registry.mark_sent(task, original["reading_id"], segment["segment_id"])
        registry.save_response(task, original["reading_id"], segment["segment_id"], output)
        registry.mark_completed(task, original["reading_id"], segment["segment_id"])
    immutable_origin = registry.get(task, original["reading_id"])
    current = new_task(bundle)
    reused = registry.prepare(current, receipt, base, materials)
    assert reused["reading_id"] != original["reading_id"] and reused["task_id"] == current["task_id"]
    assert reused["reused_from"] == {k: immutable_origin[k] for k in ("reading_id", "task_id", "attempt", "model_identity")}
    assert all(s["state"] == "completed" for s in reused["segments"])
    assert reused["segments"][0]["saved_model_output"] == output["output"]
    context = reused["segments"][0]["saved_model_output_context"]
    assert context["original_reading_id"] == original["reading_id"]
    assert context["current_reading_id"] == reused["reading_id"]
    assert registry.get(task, original["reading_id"]) == immutable_origin
    assert registry.prepare(current, receipt, base, materials) == reused
    with pytest.raises(SourceError):
        registry.get(current, original["reading_id"])
    with pytest.raises(SourceError):
        registry.resolve_evidence(current, "old-id", [observation(original)])
    refs = registry.resolve_evidence(current, "new-id", [observation(reused)])
    wiki, pub = publish(registry, refs)
    assert wiki.read_reference(pub["publish_id"])["reading_id"] == reused["reading_id"]


@pytest.mark.parametrize("condition", ["incomplete", "completion_only", "other_config", "other_document", "withdrawn", "tampered"])
def test_cross_task_cache_rejects_ineligible_origins(bundle, condition):
    registry, _, task, receipt, base, materials, original = bundle
    complete(bundle, 0, save=condition != "completion_only")
    if condition != "incomplete":
        complete(bundle, 1, save=condition != "completion_only")
    current = new_task(bundle, "config-2" if condition == "other_config" else None)
    if condition == "other_config":
        materials = prepare_pdf_materials(registry.workspace, materials.original_relpath,
            document_id=receipt["document_id"], revision_id=receipt["revision_id"], parse_id=base["parse_id"],
            config_revision=current["config_revision"], budget=PdfBudget(max_pages=1))
    if condition == "other_document":
        receipt = registry.source.receive(registry.workspace / materials.original_relpath, filename="same.pdf",
            request_id="different-document", origin=task["origin"])
        registry.source.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="测试")
        base = registry.source.parse(receipt["revision_id"], prepare_only=True)
        materials = prepare_pdf_materials(registry.workspace, materials.original_relpath,
            document_id=receipt["document_id"], revision_id=receipt["revision_id"], parse_id=base["parse_id"],
            config_revision=current["config_revision"], budget=PdfBudget(max_pages=1))
    if condition == "withdrawn":
        registry.source.change(receipt["document_id"], action="withdraw", expected_state=1, reason="撤回")
    if condition == "tampered":
        with registry.source.transaction() as db:
            stored = json.loads(db.execute("SELECT record FROM reading_records WHERE id=?", (original["reading_id"],)).fetchone()[0])
            stored["segments"][0]["saved_model_output"] = []
            db.execute("UPDATE reading_records SET record=? WHERE id=?", (json.dumps(stored), original["reading_id"]))
    if condition in ("withdrawn", "tampered"):
        with pytest.raises(SourceError):
            registry.prepare(current, receipt, base, materials)
    else:
        fresh = registry.prepare(current, receipt, base, materials)
        assert "reused_from" not in fresh
        assert fresh["reading_id"] != original["reading_id"]
        assert all(s["state"] == "prepared" for s in fresh["segments"])


def test_persisted_segment_range_cannot_override_manifest(bundle):
    registry, _, task, *_, record = bundle
    complete(bundle)
    with registry.source.transaction() as db:
        row = json.loads(db.execute("SELECT record FROM reading_records WHERE id=?", (record["reading_id"],)).fetchone()[0])
        row["segments"][0]["page_map"] = [1, 2, 99]
        db.execute("UPDATE reading_records SET record=? WHERE id=?", (json.dumps(row), record["reading_id"]))
    with pytest.raises(SourceError) as error:
        registry.resolve_evidence(task, "tampered-range", [observation(record)])
    assert error.value.code == "storage_failed"
