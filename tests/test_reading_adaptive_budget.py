"""真实 PDF/TaskStore/SQLite 的离线缩批接续；不调用模型。"""
import copy
import shutil
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfReader, PdfWriter

from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.reading import ReadingService, verify_reading_unit
from agent.knowledge.reading_materials import (
    PdfBudget, ReadingMaterialError, load_pdf_materials, prepare_pdf_materials, replan_pdf_segment,
)
from agent.knowledge.reading_publication import register_target, publication_state
from common.personal_tasks import OWNER, TaskError, TaskStore


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("不得出网"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "adaptive", {}, {"channel": "test"}, "cfg-1")
    writer = PdfWriter()
    for page in range(1, 9):
        writer.add_blank_page(width=200 + page, height=300)
    path = tmp_path / "source.pdf"
    with path.open("wb") as stream:
        writer.write(stream)
    store.attach(OWNER, task["task_id"], path)
    task = store.claim(OWNER, task["task_id"])
    source = SourceService(workspace)
    receipt = source.receive(path, filename=path.name, request_id="source", origin=task["origin"])
    source.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="测试")
    base = source.parse(receipt["revision_id"], prepare_only=True)
    materials = prepare_pdf_materials(workspace, ".personal_sources/originals/" + receipt["sha256"],
        document_id=receipt["document_id"], revision_id=receipt["revision_id"], parse_id=base["parse_id"],
        config_revision=task["config_revision"], budget=PdfBudget(max_pages=4))
    registry = ReadingService(workspace, task_store=store)
    record = registry.prepare(task, receipt, base, materials)
    register_target(store, task, [f"{receipt['revision_id']}:{p}" for p in range(1, 9)])
    return registry, store, task, materials, record


def complete(registry, task, record, index=0):
    sid = record["segments"][index]["segment_id"]
    registry.mark_sent(task, record["reading_id"], sid)
    registry.save_response(task, record["reading_id"], sid,
        dict(id="response-" + sid, status="completed", output=[], usage={"input_tokens": 20}))
    return registry.mark_completed(task, record["reading_id"], sid)


def fail(registry, task, record, index=1, sent=True):
    sid = record["segments"][index]["segment_id"]
    if sent:
        registry.mark_sent(task, record["reading_id"], sid)
        registry.mark_unknown(task, record["reading_id"], sid)
    registry.mark_budget_exceeded(task, record["reading_id"], sid,
        error_code="reading_budget_exceeded", budget_kind="input_context")
    return sid


def evidence(record, index=0):
    segment = record["segments"][index]
    return dict(reading_id=record["reading_id"], evidence_key="figure", segment_id=segment["segment_id"],
        location=dict(kind="pdf", physical_page=segment["target_pages"][0]),
        observation="图中包含一张结构图", type="visual_interpretation")


def test_exact_partition_old_proof_and_task_goal_survive(bundle):
    registry, store, task, materials, record = bundle
    complete(registry, task, record)
    old_refs = registry.resolve_evidence(task, "old", [evidence(record)])
    sid = fail(registry, task, record)
    before = registry.get(task, record["reading_id"])
    ledger = publication_state(store, task)
    old_files = {s.material_relpath: (registry.workspace / s.material_relpath).read_bytes()
                 for s in materials.segments}
    child = registry.replan_budget_exceeded(task, record["reading_id"], sid)
    assert child["reading_id"] != record["reading_id"]
    assert child["manifest_hash"] != record["manifest_hash"]
    assert child["continuation_from"]["manifest_hash"] == record["manifest_hash"]
    assert [s["target_pages"] for s in child["segments"]] == [[5, 6], [7, 8]]
    for segment in child["segments"]:
        assert segment["state"] == "prepared"
        assert not segment.get("delivery_receipt_id")
        media = registry.load(task, child["reading_id"], segment["segment_id"])
        pages = PdfReader(media["path"]).pages
        assert [int(p.mediabox.width) - 200 for p in pages] == segment["target_pages"]
        assert media["page_map"] == segment["target_pages"]
    assert registry.get(task, record["reading_id"]) == before
    assert publication_state(store, task) == ledger
    assert all((registry.workspace / p).read_bytes() == data for p, data in old_files.items())
    assert registry.resolve_evidence(task, "old", [evidence(record)]) == old_refs
    ref = next(iter(old_refs.values()))
    parsed = registry._parse(ref["parse_id"], record)
    with registry.source.transaction() as db:
        proof = verify_reading_unit(db, registry.source, parsed["units"][ref["ordinal"]], ref)
    assert proof["reading_id"] == record["reading_id"]
    complete(registry, task, child)
    assert registry.resolve_evidence(task, "new", [evidence(child)])
    with pytest.raises(SourceError):
        registry.resolve_evidence(task, "failed", [evidence(record, 1)])


@pytest.mark.parametrize("code,kind", [
    ("upstream_stream_break", "input_context"), ("reading_budget_exceeded", "output_tokens"),
    ("reading_budget_exceeded", "unknown"), ("context_length_exceeded", "input_context"),
    ("timeout", "input_bytes"),
])
def test_only_explicit_input_budget_errors_are_eligible(bundle, code, kind):
    registry, _, task, _, record = bundle
    sid = record["segments"][1]["segment_id"]
    registry.mark_sent(task, record["reading_id"], sid)
    registry.mark_unknown(task, record["reading_id"], sid)
    before = registry.get(task, record["reading_id"])
    with pytest.raises(SourceError):
        registry.mark_budget_exceeded(task, record["reading_id"], sid, error_code=code, budget_kind=kind)
    with pytest.raises(SourceError):
        registry.replan_budget_exceeded(task, record["reading_id"], sid)
    assert registry.get(task, record["reading_id"]) == before
    assert len(registry.for_task(task)) == 1


@pytest.mark.parametrize("state", ["prepared", "sent", "completed", "saved"])
def test_nonfailed_and_saved_products_cannot_be_replanned(bundle, state):
    registry, _, task, _, record = bundle
    sid = record["segments"][1]["segment_id"]
    if state == "completed":
        complete(registry, task, record, 1)
    elif state in ("sent", "saved"):
        registry.mark_sent(task, record["reading_id"], sid)
        if state == "saved":
            registry.save_response(task, record["reading_id"], sid,
                dict(id="response-saved", status="completed", output=[]))
    before = registry.get(task, record["reading_id"])
    with pytest.raises(SourceError):
        registry.replan_budget_exceeded(task, record["reading_id"], sid)
    if state in ("saved", "completed"):
        with pytest.raises(SourceError):
            registry.mark_budget_exceeded(task, record["reading_id"], sid,
                error_code="reading_budget_exceeded", budget_kind="input_bytes")
    assert registry.get(task, record["reading_id"]) == before


def test_concurrent_replay_restart_and_nested_split(bundle):
    registry, store, task, _, record = bundle
    sid = fail(registry, task, record, sent=False)
    with ThreadPoolExecutor(max_workers=3) as pool:
        children = list(pool.map(lambda _: registry.replan_budget_exceeded(task, record["reading_id"], sid), range(3)))
    assert children[0] == children[1] == children[2]
    child = children[0]
    complete(registry, task, child, 0)
    child_sid = fail(registry, task, child, 1)
    reopened = ReadingService(registry.workspace, task_store=store)
    grandchild = reopened.replan_budget_exceeded(task, child["reading_id"], child_sid)
    assert [s["target_pages"] for s in grandchild["segments"]] == [[7], [8]]
    last = fail(reopened, task, grandchild, 0)
    before = reopened.for_task(task)
    with pytest.raises(ReadingMaterialError) as error:
        reopened.replan_budget_exceeded(task, grandchild["reading_id"], last)
    assert error.value.code == "reading_budget_exceeded"
    assert reopened.for_task(task) == before
    assert reopened.replan_budget_exceeded(task, record["reading_id"], sid)["reading_id"] == child["reading_id"]


def test_new_attempt_resumes_even_all_failed_parent_without_resending_success(bundle):
    registry, store, task, _, record = bundle
    sid = fail(registry, task, record, sent=False)
    child = registry.replan_budget_exceeded(task, record["reading_id"], sid)
    complete(registry, task, child, 0)
    store.fail(OWNER, task["task_id"], task["attempt"], "interrupted", "read")
    store.retry(OWNER, task["task_id"], task["attempt"])
    current = store.claim(OWNER, task["task_id"])
    continued = registry.replan_budget_exceeded(current, record["reading_id"], sid)
    assert continued["reading_id"] == child["reading_id"]
    assert continued["attempt"] == task["attempt"]
    assert continued["segments"][0]["state"] == "completed"
    assert continued["segments"][1]["state"] == "prepared"
    with pytest.raises(SourceError):
        registry.load(current, child["reading_id"], child["segments"][0]["segment_id"])
    assert registry.load(current, child["reading_id"], child["segments"][1]["segment_id"])
    assert registry.get(current, record["reading_id"])["manifest"] == record["manifest"]
    with pytest.raises(TaskError):
        registry.mark_budget_exceeded(task, record["reading_id"], sid,
            error_code="reading_budget_exceeded", budget_kind="input_bytes")


def test_source_tamper_and_cross_task_are_rejected(bundle):
    registry, store, task, materials, record = bundle
    sid = fail(registry, task, record)
    other = store.submit(OWNER, "other", {}, {"channel": "test"}, task["config_revision"])
    other = store.claim(OWNER, other["task_id"])
    with pytest.raises(SourceError):
        registry.replan_budget_exceeded(other, record["reading_id"], sid)
    (registry.workspace / materials.original_relpath).write_bytes(b"corrupt")
    with pytest.raises(ReadingMaterialError):
        registry.replan_budget_exceeded(task, record["reading_id"], sid)


def test_overlap_with_success_is_refused_without_silently_shrinking_scope(bundle):
    registry, _, task, materials, record = bundle
    overlapping = prepare_pdf_materials(registry.workspace, materials.original_relpath,
        document_id=materials.document_id, revision_id=materials.revision_id, parse_id=materials.parse_id,
        config_revision=materials.config_revision, target_pages=[5])
    other = registry.prepare(task, dict(document_id=materials.document_id, revision_id=materials.revision_id),
        materials.parse_id, overlapping)
    complete(registry, task, other)
    sid = fail(registry, task, record)
    before = registry.for_task(task)
    with pytest.raises(SourceError, match="对账"):
        registry.replan_budget_exceeded(task, record["reading_id"], sid)
    assert registry.for_task(task) == before


def test_sparse_targets_close_overlap_and_restore_under_new_root(bundle, tmp_path):
    registry, _, _, materials, _ = bundle
    sparse = prepare_pdf_materials(registry.workspace, materials.original_relpath,
        document_id=materials.document_id, revision_id=materials.revision_id, parse_id=materials.parse_id,
        config_revision=materials.config_revision, target_pages=[2, 4, 6], overlap_pages=1)
    before = copy.deepcopy(sparse.to_dict())
    child = replan_pdf_segment(registry.workspace, sparse, sparse.segments[0].segment_id)
    assert child.target_pages == (2, 4, 6)
    assert [s.page_map for s in child.segments] == [(2,), (4,), (6,)]
    assert child.budget.reserved_output_tokens == sparse.budget.reserved_output_tokens
    assert child.budget.reserved_input_tokens == sparse.budget.reserved_input_tokens
    assert sparse.to_dict() == before
    restored = tmp_path / "restored"
    shutil.copytree(registry.workspace, restored)
    assert load_pdf_materials(restored, child.to_dict(), expected_manifest_hash=child.manifest_hash) == child


def test_material_write_failure_can_retry_without_partial_record(bundle, monkeypatch):
    import agent.knowledge.reading_materials as module
    registry, store, task, _, record = bundle
    sid = fail(registry, task, record)
    real_store = module._store
    calls = []

    def interrupted(*args):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("模拟材料写入中断")
        return real_store(*args)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_store", interrupted)
        with pytest.raises(OSError):
            registry.replan_budget_exceeded(task, record["reading_id"], sid)
    assert len(registry.for_task(task)) == 1
    reopened = ReadingService(registry.workspace, task_store=store)
    child = reopened.replan_budget_exceeded(task, record["reading_id"], sid)
    assert [s["target_pages"] for s in child["segments"]] == [[5, 6], [7, 8]]


def test_cancellation_and_config_change_cannot_replan(bundle):
    registry, store, task, _, record = bundle
    sid = fail(registry, task, record)
    altered = dict(task, config_revision="another-config")
    with pytest.raises(SourceError):
        registry.replan_budget_exceeded(altered, record["reading_id"], sid)
    store.cancel(OWNER, task["task_id"], task["attempt"])
    with pytest.raises(TaskError):
        registry.replan_budget_exceeded(task, record["reading_id"], sid)
    with registry.source.transaction() as db:
        assert db.execute("SELECT count(*) FROM reading_records").fetchone()[0] == 1


def test_existing_child_material_must_be_intact_on_replay(bundle):
    registry, _, task, _, record = bundle
    sid = fail(registry, task, record)
    child = registry.replan_budget_exceeded(task, record["reading_id"], sid)
    path = child["manifest"]["segments"][0]["material_relpath"]
    (registry.workspace / path).write_bytes(b"corrupt")
    with pytest.raises(ReadingMaterialError):
        registry.replan_budget_exceeded(task, record["reading_id"], sid)
    with registry.source.transaction() as db:
        assert db.execute("SELECT count(*) FROM reading_records").fetchone()[0] == 2


def test_budget_kind_cannot_be_changed_after_checkpoint(bundle):
    registry, _, task, _, record = bundle
    sid = fail(registry, task, record, sent=False)
    with pytest.raises(SourceError) as error:
        registry.mark_budget_exceeded(task, record["reading_id"], sid,
            error_code="reading_budget_exceeded", budget_kind="input_bytes")
    assert error.value.code == "idempotency_conflict"
