"""Office →阅读 PDF→registry→真实出处的离线契约；只替换转换进程。"""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest
from docx import Document
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter

from agent.knowledge.office_reading_flow import prepare_office_read, prepare_office_task_target
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.parser import SourceError, parse_source
from agent.knowledge.reading import ReadingService
from agent.knowledge.reading_materials import (
    PdfBudget, ReadingMaterialError, load_pdf_materials, prepare_pdf_materials, verify_pdf_materials,
)
from agent.knowledge.reading_publication import publication_state
from agent.knowledge.wiki import WikiService
from common.personal_tasks import OWNER, TaskError, TaskStore
from models.config_revision import capture_config_revision
from tests.test_reading_capabilities import ProbeModel


P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def powerpoint(path):
    # 真实 OOXML 包供生产结构 parser 使用；转换仍仅由进程替身完成。
    presentation = Presentation()
    for number in range(1, 4):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(0, 0, Inches(3), Inches(1)).text = f"幻灯片结构正文 {number}"
        if number == 1:
            slide.notes_slide.notes_text_frame.text = "独立讲稿备注"
        if number == 2:
            slide._element.set("show", "0")
    for entry, slide_id in zip(presentation._element.sldIdLst, (901, 322, 703)):
        entry.id = slide_id
    presentation.save(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("不得出网"))
    executable = tmp_path / "soffice.exe"
    executable.write_bytes(b"offline-engine")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout=b"LibreOffice test-1")
        assert "--headless" in args and kwargs["timeout"] > 0 and "shell" not in kwargs
        source = Path(args[-1])
        writer = PdfWriter()
        if source.suffix == ".pptx":
            with zipfile.ZipFile(source) as archive:
                listing = ET.fromstring(archive.read("ppt/presentation.xml")).find(f"{{{P}}}sldIdLst")
                assert len(listing) == 1
                width, count = int(listing[0].get("id")), 1
        else:
            assert Document(source).paragraphs[0].text == "Word 资料原文"
            width, count = 200, 3
        for number in range(count):
            writer.add_blank_page(width=width + number, height=300)
        writer.write(Path(args[args.index("--outdir") + 1]) / (source.stem + ".pdf"))
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", run)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "office", {}, {"channel": "test"}, capture_config_revision())
    return workspace, store, task, executable, calls


def setup(env, formats=("docx",), transform=None):
    workspace, store, task, executable, calls = env
    source = SourceService(workspace)
    receipts = []
    decisions = {}
    for index, suffix in enumerate(formats):
        path = workspace.parent / f"source-{index}.{suffix}"
        if suffix == "docx":
            document = Document()
            document.add_paragraph("Word 资料原文")
            document.save(path)
        elif suffix == "pptx":
            powerpoint(path)
        else:
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.write(path)
        if transform:
            transform(path)
        entry = store.attach(OWNER, task["task_id"], path)
        receipt = source.receive(path, filename=path.name, request_id=str(index), origin=task["origin"])
        source.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="测试")
        decisions[entry["attachment_id"]] = dict(purpose="long_term", source=dict(
            document_id=receipt["document_id"], revision_id=receipt["revision_id"]))
        receipts.append(receipt)
    task = store.claim(OWNER, task["task_id"])
    task = store._change(OWNER, task["task_id"], task["attempt"], {"running"}, attachment_decisions=decisions)
    return task, receipts


def prepare(env, task, receipt, **kwargs):
    workspace, store, _, executable, _ = env
    return prepare_office_read(store, task, workspace, receipt, ProbeModel(), soffice=executable, **kwargs)


def complete(registry, task, record, index=0):
    sid = record["segments"][index]["segment_id"]
    registry.mark_sent(task, record["reading_id"], sid)
    registry.save_response(task, record["reading_id"], sid,
        dict(id="resp-" + sid, status="completed", output=[], usage={"input_tokens": 20}))
    return registry.mark_completed(task, record["reading_id"], sid)


def observation(record, location):
    return dict(reading_id=record["reading_id"], segment_id=record["segments"][0]["segment_id"],
        evidence_key="figure", location=location, observation="图中呈现业务流程", type="visual_interpretation")


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
def test_original_identity_registry_observation_and_wiki_readback(env, suffix):
    workspace, store, _, _, calls = env
    task, receipts = setup(env, (suffix,))
    receipt = receipts[0]
    original = workspace / ".personal_sources/originals" / receipt["sha256"]
    before = original.read_bytes()
    task, parsed, materials, registry, identity = prepare(env, task, receipt, budget=PdfBudget(max_pages=1))
    assert identity["mime_type"] == "application/pdf"
    assert parsed["parser_id"] == "office-native-reading" and parsed["units"]
    assert parsed["parser_version"] == "2" and parsed["config"]["preparation"] == "native-office-v2"
    assert parsed["sha256"] == materials.source_hash == receipt["sha256"]
    assert materials.original_hash != receipt["sha256"]
    assert materials.source_relpath == ".personal_sources/originals/" + receipt["sha256"]
    office = materials.derived_source["office_manifest"]
    work = workspace / office["original_relpath"]
    assert work.suffix == "." + suffix and work.read_bytes() == before == original.read_bytes()
    assert original.suffix == "" and not str(work).endswith(receipt["filename"])
    assert office["coverage_gaps"]
    record = registry.prepare(task, receipt, parsed, materials)
    ledger = publication_state(store, task)
    assert set(ledger["targets"]) == set(materials.coverage_keys())
    media = registry.load(task, record["reading_id"], record["segments"][0]["segment_id"])
    assert media["source_page_map"] == [materials.location(1)]
    if suffix == "docx":
        assert materials.location(1) == dict(kind="word_reading_copy_page", reading_copy_page=1)
        assert ":word:" in materials.coverage_keys()[0]
    else:
        assert materials.location(1) == dict(kind="slide", source_slide_id="901", source_slide_number=1)
        assert materials.location(2)["source_slide_number"] == 3
        assert any("322" in gap for gap in media["coverage_gaps"])
        assert media["notes"][0]["text"] == "独立讲稿备注"
        assert materials.coverage_keys() == [f"{receipt['revision_id']}:slide:901", f"{receipt['revision_id']}:slide:703"]
    with pytest.raises(SourceError):
        registry.resolve_evidence(task, "undelivered", [observation(record, materials.location(1))])
    complete(registry, task, record)
    assert registry.coverage(task, record["reading_id"], record["segments"][0]["segment_id"]) == materials.coverage_keys([1])
    refs = registry.resolve_evidence(task, "office-obs", [observation(record, materials.location(1))])
    ref = next(iter(refs.values()))
    saved = json.loads((registry.source.root / "parses" / (ref["parse_id"] + ".json")).read_text(encoding="utf-8"))
    assert saved["sha256"] == receipt["sha256"]
    wiki = WikiService(workspace)
    batch = wiki.prepare([dict(title="Office 主题", summary="介绍业务流程。", category="测试",
        blocks=[dict(content="资料图中为业务流程", sources=[ref])])])
    published = wiki.publish(batch["batch_id"])[0]
    readback = wiki.read_reference(published["publish_id"])
    assert readback["location"] == dict(materials.location(1), type="visual_interpretation")
    assert readback["filename"] == receipt["filename"]
    assert readback["reading_copy"]["source_page_map"] == [materials.location(1)]
    assert readback["reading_copy"]["coverage_gaps"] == office["coverage_gaps"]
    assert original.read_bytes() == before
    before_calls = len(calls)
    again = prepare(env, task, receipt, budget=PdfBudget(max_pages=1))
    assert len(calls) == before_calls  # 任务锚定排版后，恢复不再调用引擎。
    assert again[2].to_dict() == materials.to_dict()
    assert again[3].prepare(task, receipt, parsed, materials)["reading_id"] == record["reading_id"]


@pytest.mark.parametrize("suffix,bad", [
    ("docx", dict(kind="pdf", physical_page=1)),
    ("docx", dict(kind="word_reading_copy_page", reading_copy_page=True)),
    ("docx", dict(kind="word_reading_copy_page", reading_copy_page=2)),
    ("pptx", dict(kind="pdf", physical_page=1)),
    ("pptx", dict(kind="slide", source_slide_id="322", source_slide_number=2)),
    ("pptx", dict(kind="slide", source_slide_id="901", source_slide_number=3)),
])
def test_fake_pdf_hidden_slide_and_outside_segment_locations_rejected(env, suffix, bad):
    task, receipts = setup(env, (suffix,))
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0], budget=PdfBudget(max_pages=1))
    record = registry.prepare(task, receipts[0], parsed, materials)
    complete(registry, task, record)
    with pytest.raises(SourceError) as error:
        registry.resolve_evidence(task, "invalid", [observation(record, bad)])
    assert error.value.code == "invalid_request"


def test_subset_does_not_reduce_mixed_task_targets_and_other_task_rejected(env):
    workspace, store, _, _, _ = env
    task, receipts = setup(env, ("docx", "pptx", "pdf"))
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0], first=2, last=2)
    assert materials.target_pages == (2,) and materials.segments[0].page_map == (2,)
    targets = publication_state(store, task)["targets"]
    assert len(targets) == 6  # Word 3 + visible PPT 2 + PDF 1
    assert set(materials.coverage_keys()) < set(targets)
    again = prepare_office_task_target(store, task, workspace, soffice=env[3])
    assert publication_state(store, again)["targets"] == targets
    other = store.submit(OWNER, "other", {}, {"channel": "test"}, task["config_revision"])
    other = store.claim(OWNER, other["task_id"])
    record = registry.prepare(task, receipts[0], parsed, materials)
    with pytest.raises(SourceError):
        registry.get(other, record["reading_id"])


@pytest.mark.parametrize("part", ["original", "work", "pdf", "manifest", "mapping", "unbound"])
def test_every_link_of_office_material_chain_is_verified(env, part):
    workspace, _, _, _, _ = env
    task, receipts = setup(env)
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0])
    derived = copy.deepcopy(materials.derived_source)
    if part in ("original", "work", "pdf"):
        relative = {"original": derived["original_relpath"], "work": derived["office_manifest"]["original_relpath"],
                    "pdf": materials.original_relpath}[part]
        (workspace / relative).write_bytes(b"tampered")
    elif part == "manifest":
        derived["office_manifest"]["coverage_gaps"] = []
        materials = replace(materials, derived_source=derived)
    elif part == "mapping":
        derived["office_manifest"]["page_map"][0]["reading_copy_page"] = 44
        materials = replace(materials, derived_source=derived)
    else:
        materials = replace(materials, derived_source=None)
    with pytest.raises((ReadingMaterialError, SourceError)):
        registry.prepare(task, receipts[0], parsed, materials)


def test_replan_keeps_office_chain_and_locations(env):
    task, receipts = setup(env)
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0])
    record = registry.prepare(task, receipts[0], parsed, materials)
    sid = record["segments"][0]["segment_id"]
    registry.mark_budget_exceeded(task, record["reading_id"], sid,
        error_code="reading_budget_exceeded", budget_kind="input_context")
    child = registry.replan_budget_exceeded(task, record["reading_id"], sid)
    restored = load_pdf_materials(registry.workspace, child["manifest"], expected_manifest_hash=child["manifest_hash"])
    assert restored.derived_source == materials.derived_source
    assert [s.target_pages for s in restored.segments] == [(1,), (2,), (3,)]
    complete(registry, task, child)
    refs = registry.resolve_evidence(task, "child", [observation(child, restored.location(1))])
    assert refs


def test_plain_pdf_shape_and_hash_are_unchanged(env):
    workspace, _, _, _, _ = env
    path = workspace / "original.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    writer.write(path)
    pdf = prepare_pdf_materials(workspace, "original.pdf", document_id="d", revision_id="r",
        parse_id="p", config_revision="c")
    legacy = asdict(pdf)
    legacy.pop("derived_source")
    legacy.pop("region_supplement")
    digest = sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert "derived_source" not in pdf.to_dict() and pdf.manifest_hash == digest
    assert pdf.location(1) == dict(kind="pdf", physical_page=1)
    assert load_pdf_materials(workspace, pdf.to_dict(), expected_manifest_hash=digest) == pdf
    assert verify_pdf_materials(workspace, pdf) is pdf


def test_registry_and_observation_restore_without_engine(env):
    workspace, store, _, _, _ = env
    task, receipts = setup(env, ("pptx",))
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0])
    record = registry.prepare(task, receipts[0], parsed, materials)
    complete(registry, task, record)
    evidence = [observation(record, materials.location(1))]
    refs = registry.resolve_evidence(task, "persisted", evidence)
    restored = workspace.parent / "restored"
    shutil.copytree(workspace, restored)
    reopened = ReadingService(restored, task_store=store)
    assert reopened.get(task, record["reading_id"])["manifest"] == materials.to_dict()
    assert reopened.resolve_evidence(task, "persisted", evidence) == refs
    (restored / materials.derived_source["original_relpath"]).write_bytes(b"corrupt-source")
    with pytest.raises(ReadingMaterialError):
        reopened.get(task, record["reading_id"])


def test_conversion_failure_does_not_register_reading_or_target(env, monkeypatch):
    workspace, store, _, _, _ = env
    task, receipts = setup(env)
    original_run = subprocess.run

    def failed(args, **kwargs):
        if "--version" in args:
            return original_run(args, **kwargs)
        return SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ValueError) as error:
        prepare(env, task, receipts[0])
    assert error.value.code == "conversion_failed"
    assert publication_state(store, task) is None
    assert ReadingService(workspace, task_store=store).for_task(task) == []


def test_cancelled_task_cannot_prepare(env):
    workspace, store, _, _, _ = env
    task, receipts = setup(env)
    store.cancel(OWNER, task["task_id"], task["attempt"])
    with pytest.raises(TaskError):
        prepare(env, task, receipts[0])
    assert not (workspace / "office_sources").exists()


def test_retry_and_engine_change_keep_pinned_layout_and_completed_pages(env, monkeypatch):
    workspace, store, _, executable, calls = env
    task, receipts = setup(env)
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0], budget=PdfBudget(max_pages=1))
    record = registry.prepare(task, receipts[0], parsed, materials)
    complete(registry, task, record)
    store.fail(OWNER, task["task_id"], task["attempt"], "interrupted", "read")
    store.retry(OWNER, task["task_id"], task["attempt"])
    current = store.claim(OWNER, task["task_id"])
    executable.unlink()
    call_count = len(calls)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("接续不能重新排版"))
    current, next_parse, next_materials, resumed, _ = prepare(env, current, receipts[0], budget=PdfBudget(max_pages=1))
    assert len(calls) == call_count and next_materials == materials
    assert next_parse == parsed
    continued = resumed.prepare(current, receipts[0], next_parse, next_materials)
    assert continued["reading_id"] == record["reading_id"]
    assert [s["state"] for s in continued["segments"]] == ["completed", "prepared", "prepared"]
    with pytest.raises(SourceError):
        resumed.load(current, continued["reading_id"], continued["segments"][0]["segment_id"])
    assert resumed.load(current, continued["reading_id"], continued["segments"][1]["segment_id"])


def test_prepare_under_new_root_uses_relative_materials_and_parse(env, monkeypatch):
    workspace, store, _, _, _ = env
    task, receipts = setup(env)
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0])
    record = registry.prepare(task, receipts[0], parsed, materials)
    complete(registry, task, record)
    moved = workspace.parent / "moved-workspace"
    workspace.rename(moved)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("恢复不能调用转换进程"))
    restored_env = (moved, *env[1:])
    _, next_parse, next_materials, reopened, _ = prepare(restored_env, task, receipts[0])
    assert next_parse == parsed and next_materials == materials
    assert reopened.prepare(task, receipts[0], next_parse, next_materials)["reading_id"] == record["reading_id"]
    assert not workspace.exists()


def test_concurrent_office_preparation_produces_one_registry_record(env):
    task, receipts = setup(env)
    # 先固定转换目录，测试准备稿／任务锚／registry 的并发，不复测转换器自身竞争。
    task = prepare_office_task_target(env[1], task, env[0], soffice=env[3])

    def attempt(_):
        current, parsed, materials, registry, _ = prepare(env, task, receipts[0])
        return registry.prepare(current, receipts[0], parsed, materials)

    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(attempt, range(3)))
    assert records[0] == records[1] == records[2]


@pytest.mark.parametrize("first,last", [(0, 1), (1, 4), (True, 1), (2, 1)])
def test_invalid_page_window_does_not_create_reading(env, first, last):
    task, receipts = setup(env)
    with pytest.raises(SourceError):
        prepare(env, task, receipts[0], first=first, last=last)
    registry = ReadingService(env[0], task_store=env[1])
    assert registry.for_task(task) == []


def test_source_withdrawal_during_conversion_does_not_register_goal(env, monkeypatch):
    workspace, store, _, _, _ = env
    task, receipts = setup(env)
    original_run = subprocess.run

    def withdraw(args, **kwargs):
        result = original_run(args, **kwargs)
        if "--convert-to" in args:
            SourceService(workspace).change(receipts[0]["document_id"], action="withdraw", expected_state=1, reason="撤回")
        return result

    monkeypatch.setattr(subprocess, "run", withdraw)
    with pytest.raises(SourceError):
        prepare(env, task, receipts[0])
    assert publication_state(store, task) is None


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
def test_native_base_retains_exact_source_structure_and_separate_notes(env, suffix):
    task, receipts = setup(env, (suffix,))
    receipt = receipts[0]
    task, parsed, materials, registry, _ = prepare(env, task, receipt, first=1, last=1)
    expected = parse_source(env[0] / materials.source_relpath, receipt["filename"], prepare_only=True)
    assert parsed["units"] == expected["units"]
    assert parsed["visual_coverage"] == expected["visual_coverage"]
    assert parsed["config"]["source_parser"] == {k: expected[k] for k in ("parser_id", "parser_version", "config")}
    assert parsed["visual_warnings"][:len(expected["visual_warnings"])] == expected["visual_warnings"]
    assert parsed["sha256"] == receipt["sha256"]
    if suffix == "pptx":
        notes = [unit for unit in parsed["units"] if unit["type"] == "speaker_notes"]
        assert len(notes) == 1 and notes[0]["text"] == "独立讲稿备注"
        assert notes[0]["layer"] == "speaker_notes" and notes[0]["origin"] == "source"
        assert any(unit.get("hidden") for unit in parsed["units"])
        assert len(parsed["visual_coverage"]) == 3  # 局部页窗不裁掉原件结构。
    else:
        assert parsed["units"][0]["location"]["kind"] == "docx"
        assert parsed["units"][0]["text"] == "Word 资料原文"
    record = registry.prepare(task, receipt, parsed, materials)
    complete(registry, task, record)
    refs = registry.resolve_evidence(task, "append", [observation(record, materials.location(1))])
    ref = next(iter(refs.values()))
    path = registry.source.root / "parses" / (ref["parse_id"] + ".json")
    appended = json.loads(path.read_text(encoding="utf-8"))
    assert ref["ordinal"] == len(expected["units"])
    assert appended["units"][:-1] == expected["units"]
    assert appended["units"][-1]["origin"] == "model"
    assert appended["sha256"] == receipt["sha256"]


def test_saved_base_and_model_observations_are_not_reparsed_or_rewritten(env, monkeypatch):
    task, receipts = setup(env, ("pptx",))
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0])
    record = registry.prepare(task, receipts[0], parsed, materials)
    complete(registry, task, record)
    item = observation(record, materials.location(1))
    refs = registry.resolve_evidence(task, "saved", [item])
    paths = [registry.source.root / "parses" / (p + ".json")
             for p in (parsed["parse_id"], next(iter(refs.values()))["parse_id"])]
    before = {p: p.read_bytes() for p in paths}
    monkeypatch.setattr("agent.knowledge.office_reading_flow.parse_source",
                        lambda *a, **kw: pytest.fail("已有锚不得重解析或改写旧 ordinal"))
    again = prepare(env, task, receipts[0])
    assert again[1] == parsed and again[2] == materials
    assert registry.resolve_evidence(task, "saved", [item]) == refs
    assert all(p.read_bytes() == value for p, value in before.items())


def test_legacy_empty_base_and_observations_keep_original_identity(env, monkeypatch):
    from agent.knowledge.personal_assistant import _hash, _json
    from agent.knowledge.reading_materials import _store

    workspace, store, _, _, _ = env
    task, receipts = setup(env)
    receipt = receipts[0]
    task = prepare_office_task_target(store, task, workspace, soffice=env[3])
    office = task["source_office_materials"][receipt["revision_id"]]
    payload = dict(parser_id="office-native-reading", parser_version="1",
        config=dict(preparation="native-office-v1", office_manifest_hash=office["manifest_hash"]),
        source_identity=dict(document_id=receipt["document_id"], revision_id=receipt["revision_id"]),
        units=[], visual_coverage=[], visual_warnings=[])
    parse_id = _hash(_json([receipt["sha256"], payload]).encode())
    legacy = dict(payload, parse_id=parse_id, document_id=receipt["document_id"],
                  revision_id=receipt["revision_id"], sha256=receipt["sha256"])
    relative = f".personal_sources/parses/{parse_id}.json"
    _store(workspace, relative, _json(legacy).encode("utf-8"))
    task = store._change(OWNER, task["task_id"], task["attempt"], {"running"},
                         source_native_parses={receipt["revision_id"]: parse_id})
    before = (workspace / relative).read_bytes()
    monkeypatch.setattr("agent.knowledge.office_reading_flow.parse_source",
                        lambda *a, **kw: pytest.fail("旧准备稿不能原地升级"))
    task, parsed, materials, registry, _ = prepare(env, task, receipt)
    assert parsed == legacy
    record = registry.prepare(task, receipt, parsed, materials)
    complete(registry, task, record)
    refs = registry.resolve_evidence(task, "legacy-observation", [observation(record, materials.location(1))])
    assert next(iter(refs.values()))["ordinal"] == 0
    assert (workspace / relative).read_bytes() == before


def test_word_revision_comment_notes_are_not_promoted_to_current_body(env):
    from agent.knowledge.personal_assistant import _hash

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def revised(path):
        with zipfile.ZipFile(path) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        tree = ET.fromstring(parts["word/document.xml"])
        paragraph = ET.fromstring(f'<w:p xmlns:w="{w}"><w:del w:id="1"><w:r><w:delText>old12dB</w:delText>'
            '</w:r></w:del><w:ins w:id="2"><w:r><w:t>new42dB</w:t></w:r></w:ins>'
            '<w:commentRangeStart w:id="9"/><w:commentRangeEnd w:id="9"/></w:p>')
        tree.find(f"{{{w}}}body").insert(1, paragraph)
        parts["word/document.xml"] = ET.tostring(tree)
        parts["word/comments.xml"] = (f'<w:comments xmlns:w="{w}"><w:comment w:id="9"><w:p><w:r>'
            '<w:t>批注不是当前正文</w:t></w:r></w:p></w:comment></w:comments>').encode("utf-8")
        relations = ET.fromstring(parts["word/_rels/document.xml.rels"])
        ET.SubElement(relations, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
                      Id="comments9", Type=R + "/comments", Target="comments.xml")
        parts["word/_rels/document.xml.rels"] = ET.tostring(relations)
        types = ET.fromstring(parts["[Content_Types].xml"])
        ET.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
            PartName="/word/comments.xml", ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml")
        parts["[Content_Types].xml"] = ET.tostring(types)
        with zipfile.ZipFile(path, "w") as archive:
            for name, value in parts.items():
                archive.writestr(name, value)

    workspace, _, _, _, _ = env
    task, receipts = setup(env, transform=revised)
    receipt = receipts[0]
    _, parsed, materials, _, _ = prepare(env, task, receipt)
    notes = materials.derived_source["office_manifest"]["notes"]
    assert any(note["kind"] == "word_revision" and note["text"] == "old12dB" for note in notes)
    assert any(note["kind"] == "word_comment" and note["text"] == "批注不是当前正文" for note in notes)
    assert all(note["current_fact"] is False and note["source_xpath"] for note in notes)
    assert [unit["text"] for unit in parsed["units"]] == ["Word 资料原文"]
    assert any(gap["reason"] == "source_structure_not_final_word_view" for gap in parsed["visual_warnings"])
    assert parsed["sha256"] == receipt["sha256"]
    assert _hash((workspace / ".personal_sources/originals" / receipt["sha256"]).read_bytes()) == receipt["sha256"]


def test_structure_parse_failure_cannot_publish_empty_fallback(env, monkeypatch):
    task, receipts = setup(env)

    def fail(*args, **kwargs):
        raise SourceError("limit_exceeded", "结构提取超限")

    monkeypatch.setattr("agent.knowledge.office_reading_flow.parse_source", fail)
    with pytest.raises(SourceError) as error:
        prepare(env, task, receipts[0])
    assert error.value.code == "limit_exceeded"
    workspace, store, *_ = env
    current = store.get(OWNER, task["task_id"])
    assert not current.get("source_native_parses")
    assert ReadingService(workspace, task_store=store).for_task(task) == []
