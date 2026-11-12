"""隔离探针的安全拒绝及进程回收回归；不启动 Office。"""

import importlib.util
import json
import os
from pathlib import Path
import time
import uuid
import zipfile

import pytest


spec = importlib.util.spec_from_file_location(
    "office_probe", Path(__file__).resolve().parents[1] / "scripts/document_reading_office_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("name,content,reason", [
    ("word/vbaProject.bin", b"inert macro marker", "active_content"),
    ("[Content_Types].xml", b'<Types><Override ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>', "active_content"),
    ("word/_rels/document.xml.rels", b'<Relationships><Relationship TargetMode="External" Target="https://example.invalid/a"/></Relationships>', "external_relationship"),
    ("word/document.xml", b'<doc><fldSimple instr="DATE"/></doc>', "dynamic_field_or_object"),
    ("word/document.xml", b'<doc><instrText>INCLUDETEXT</instrText></doc>', "dynamic_field_or_object"),
    ("ppt/slides/slide1.xml", b'<slide><oleObj/></slide>', "dynamic_field_or_object"),
    ("word/embeddings/object.bin", b"inert", "active_content"),
    ("word/document.xml", b'<!DOCTYPE a [<!ENTITY x "a">]><a/>', "xml_entity"),
])
def test_active_content_rejected_before_office(tmp_path, name, content, reason):
    path = tmp_path / "unsafe.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, content)
    assert reason in probe.preflight(path)


def test_invalid_and_legacy_rejected(tmp_path):
    path = tmp_path / "corrupt.docx"
    path.write_bytes(b"not zip")
    assert probe.preflight(path) == ["invalid_package"]
    assert probe.preflight(tmp_path / "old.doc") == ["unsupported_format"]


def test_sample_safety_and_additional_layers():
    fixtures = probe.BASE / "fixtures"
    if not fixtures.exists():
        pytest.skip("先使用项目 Python 生成夹具")
    for suffix in ("docx", "pptx"):
        assert probe.preflight(fixtures / ("sample." + suffix)) == []
    with zipfile.ZipFile(fixtures / "sample.docx") as archive:
        assert "word/comments.xml" in archive.namelist()
        assert b"<w:ins" in archive.read("word/document.xml")
        assert b"<w:del" in archive.read("word/document.xml")
    with zipfile.ZipFile(fixtures / "sample.pptx") as archive:
        assert b'show="0"' in archive.read("ppt/slides/slide2.xml")
        assert "ppt/notesSlides/notesSlide2.xml" in archive.namelist()


def test_pdf_export_does_not_certify_hidden_slide_mapping(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "slides.pdf"
    with fitz.open() as pdf:
        for label in ("slide 1", "slide 3"):
            page = pdf.new_page()
            page.insert_text((40, 40), label)
        pdf.save(path)
    evidence = {"status": "converted", "mapping": [
        {"slide": i, "reading_pdf_page": i} for i in (1, 2, 3)]}
    probe.check_pdf(path, evidence)
    assert evidence["status"] == "converted_with_gap"
    assert evidence["pdf_checks"]["slide_mapping_verified"] is False
    assert all("reading_pdf_page" not in item for item in evidence["mapping"])


@pytest.mark.skipif(os.name != "nt", reason="Windows Job")
def test_timeout_reaps_only_job_descendants():
    pytest.importorskip("win32job")
    import subprocess
    import sys
    import win32api
    import win32event

    directory = probe.BASE / ("timeout-test-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    result = directory / "child.json"
    config = directory / "config.json"
    probe.dump(config, {"job_name": "CowAgentProbeTest-" + uuid.uuid4().hex,
                        "hang": True, "result": str(result)})
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        run = probe.isolated_worker(config, timeout=2)
        assert run["timed_out"]
        assert unrelated.poll() is None
        child_pid = json.loads(result.read_text(encoding="utf-8"))["descendant_pid"]
        try:
            handle = win32api.OpenProcess(0x100000, False, child_pid)
        except Exception as exc:
            assert getattr(exc, "winerror", None) == 87
        else:
            try:
                assert win32event.WaitForSingleObject(handle, 5000) == win32event.WAIT_OBJECT_0
            finally:
                handle.Close()
        probe.dump(directory / "assertions.json", {"timeout": run, "unrelated_survived": True,
                                                    "descendant_pid": child_pid, "descendant_reaped": True,
                                                    "timestamp": time.time()})
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
