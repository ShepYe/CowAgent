"""离线契约测试：引擎替身只模拟进程边界，不模拟材料与映射实现。"""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest
from pypdf import PdfWriter

from agent.knowledge.office_materials import (
    OfficeMaterialError, load_office_materials, prepare_office_materials, verify_office_materials,
)
from agent.knowledge import office_materials as office


@pytest.fixture
def engine(tmp_path, monkeypatch):
    executable = tmp_path / "soffice.exe"
    executable.write_bytes(b"test-engine")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout=b"LibreOffice test-1")
        source = Path(args[-1])
        assert source.is_relative_to(tmp_path / "office_materials")
        assert "--headless" in args and kwargs["timeout"] > 0
        assert "shell" not in kwargs
        if source.suffix == ".pptx":
            with zipfile.ZipFile(source) as archive:
                tree = ET.fromstring(archive.read("ppt/presentation.xml"))
                listing = tree.find("{http://schemas.openxmlformats.org/presentationml/2006/main}sldIdLst")
                assert len(listing) == 1
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=300)
        writer.write(Path(args[args.index("--outdir") + 1]) / (source.stem + ".pdf"))
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", run)
    return executable, calls


def word(root, text="original-word"):
    with zipfile.ZipFile(root / "source.docx", "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')


def ppt(root):
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(root / "source.pptx", "w") as z:
        z.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst>'
                   '<p:sldId id="901" r:id="b"/><p:sldId id="222" r:id="a"/>'
                   '<p:sldId id="703" r:id="c"/></p:sldIdLst></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels", '<Relationships>'
                   f'<Relationship Id="a" Target="slides/slide1.xml" Type="{r}/slide"/>'
                   f'<Relationship Id="b" Target="slides/slide8.xml" Type="{r}/slide"/>'
                   f'<Relationship Id="c" Target="slides/slide3.xml" Type="{r}/slide"/></Relationships>')
        for number in (1, 8, 3):
            z.writestr(f"ppt/slides/slide{number}.xml", f'<p:sld xmlns:p="{p}" show="{0 if number == 1 else 1}"/>')
        z.writestr("ppt/slides/_rels/slide8.xml.rels", '<Relationships>'
                   f'<Relationship Id="n" Target="../notesSlides/notesSlide4.xml" Type="{r}/notesSlide"/></Relationships>')
        z.writestr("ppt/notesSlides/notesSlide4.xml", f'<p:notes xmlns:p="{p}" xmlns:a="{a}">'
                   '<p:sp><p:ph type="body"/><a:p><a:r><a:t>独立备注</a:t></a:r></a:p></p:sp>'
                   '<p:sp><p:ph type="sldNum"/><a:p><a:r><a:t>99</a:t></a:r></a:p></p:sp></p:notes>')


def test_word_hash_location_cache_and_restore(tmp_path, engine):
    word(tmp_path)
    before = (tmp_path / "source.docx").read_bytes()
    executable, calls = engine
    result = prepare_office_materials(tmp_path, "source.docx", soffice=executable)
    assert (tmp_path / "source.docx").read_bytes() == before
    assert result.location(1) == dict(pdf_page=1, kind="word_reading_copy_page", reading_copy_page=1)
    for invalid in (0, -1, 2, True, "1"):
        with pytest.raises(OfficeMaterialError, match="越界"):
            result.location(invalid)
    again = prepare_office_materials(tmp_path, "source.docx", soffice=executable)
    assert again.to_dict() == result.to_dict()
    assert sum("--convert-to" in call for call in calls) == 1
    restored = tmp_path / "restored"
    restored.mkdir()
    shutil.copy(tmp_path / "source.docx", restored)
    shutil.copytree(tmp_path / "office_materials", restored / "office_materials")
    loaded = load_office_materials(restored, result.to_dict(), expected_manifest_hash=result.manifest_hash)
    assert loaded == result
    assert str(tmp_path) not in json.dumps(result.to_dict())
    (restored / "source.docx").write_bytes(b"changed")
    with pytest.raises(OfficeMaterialError) as error:
        verify_office_materials(restored, result)
    assert error.value.code == "source_changed"


def test_ppt_stable_id_source_order_hidden_and_notes(tmp_path, engine):
    ppt(tmp_path)
    before = (tmp_path / "source.pptx").read_bytes()
    result = prepare_office_materials(tmp_path, "source.pptx", soffice=engine[0])
    assert result.page_count == 2
    assert [result.location(p)["source_slide_id"] for p in (1, 2)] == ["901", "703"]
    assert [x["source_slide_number"] for x in result.page_map] == [1, 3]
    assert result.notes[0]["text"] == "独立备注"
    assert result.notes[0]["source_slide_id"] == "901"
    assert any("222" in gap for gap in result.coverage_gaps)
    assert (tmp_path / "source.pptx").read_bytes() == before
    mapping = [dict(page) for page in result.page_map]
    mapping[0]["source_slide_id"] = "703"
    with pytest.raises(OfficeMaterialError) as error:
        verify_office_materials(tmp_path, replace(result, page_map=tuple(mapping)))
    assert error.value.code == "invalid_manifest"


@pytest.mark.parametrize("failure,code", [("timeout", "conversion_timeout"), ("exit", "conversion_failed"),
                                        ("missing", "conversion_failed"), ("broken", "invalid_pdf")])
def test_failure_never_publishes_cache(tmp_path, engine, monkeypatch, failure, code):
    word(tmp_path)
    original_run = subprocess.run

    def run(args, **kwargs):
        if "--version" in args:
            return original_run(args, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 1)
        if failure == "broken":
            (Path(args[args.index("--outdir") + 1]) / "source.pdf").write_bytes(b"broken")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert error.value.code == code
    assert not list((tmp_path / "office_materials").rglob("manifest.json"))
    assert not list((tmp_path / "office_materials").glob(".office-work-*"))


def test_tampered_cache_and_manifest_rejected(tmp_path, engine):
    word(tmp_path)
    result = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    manifest = result.to_dict()
    manifest["page_map"][0]["reading_copy_page"] = 72
    with pytest.raises(OfficeMaterialError) as error:
        load_office_materials(tmp_path, manifest, expected_manifest_hash=result.manifest_hash)
    assert error.value.code == "invalid_manifest"
    (tmp_path / result.material_relpath).write_bytes(b"tampered")
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert error.value.code == "cache_invalid"


def test_source_and_engine_changes_invalidate_cache(tmp_path, engine, monkeypatch):
    word(tmp_path)
    first = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    word(tmp_path, "new revision")
    second = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert second.cache_key != first.cache_key
    engine[0].write_bytes(b"upgraded engine")
    third = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert third.cache_key != second.cache_key


@pytest.mark.parametrize("relative", ["../source.docx", "C:/source.docx", "a\\source.docx", "/source.docx"])
def test_invalid_relative_identity(tmp_path, relative):
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, relative)
    assert error.value.code == "invalid_path"


def test_missing_engine_and_legacy_format(tmp_path):
    word(tmp_path)
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, "source.docx", soffice="nonexistent-office-engine-123")
    assert error.value.code == "engine_unavailable"
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, "source.doc")
    assert error.value.code == "unsupported_format"


def test_ppt_multiple_pages_refuses_guessed_mapping(tmp_path, engine, monkeypatch):
    ppt(tmp_path)
    original_run = subprocess.run

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if "--convert-to" in args:
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=300)
            writer.add_blank_page(width=200, height=300)
            writer.write(Path(args[args.index("--outdir") + 1]) / "source.pdf")
        return result

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, "source.pptx", soffice=engine[0])
    assert error.value.code == "mapping_failed"
    assert not list((tmp_path / "office_materials").rglob("manifest.json"))


def test_engine_version_change_invalidates_cache(tmp_path, engine, monkeypatch):
    word(tmp_path)
    first = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    original_run = subprocess.run

    def run(args, **kwargs):
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout=b"LibreOffice test-2")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    second = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert second.cache_key != first.cache_key


def test_source_changes_during_conversion_not_published(tmp_path, engine, monkeypatch):
    word(tmp_path)
    original_run = subprocess.run

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if "--convert-to" in args:
            (tmp_path / "source.docx").write_bytes(b"concurrent edit")
        return result

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert error.value.code == "source_changed"
    assert not list((tmp_path / "office_materials").rglob("manifest.json"))


def revised_word(root, content):
    w = office.W
    with zipfile.ZipFile(root / "source.docx", "w") as z:
        for part in ("document", "header1", "footer1", "footnotes", "endnotes"):
            z.writestr(f"word/{part}.xml", f'<w:document xmlns:w="{w}" '
                       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
                       'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14">'
                       f'<w:body>{content}</w:body></w:document>')
        z.writestr("word/comments.xml", f'<w:comments xmlns:w="{w}"><w:comment w:id="9">'
                   '<w:p><w:r><w:t>历史批注</w:t></w:r></w:p></w:comment></w:comments>')
        z.writestr("word/media/image1.png", b"unchanged-image")


def test_final_view_work_copy_and_locatable_additional_layers(tmp_path, engine, monkeypatch):
    revised_word(tmp_path, '<w:p><w:pPr><w:pPrChange w:id="8"><w:pPr><w:jc w:val="right"/></w:pPr></w:pPrChange>'
                 '<w:jc w:val="left"/></w:pPr><w:del w:id="1"><w:r><w:delText>旧预算 12 dB</w:delText></w:r></w:del>'
                 '<w:ins w:id="2"><w:r><w:t>新预算 42 dB</w:t></w:r></w:ins>'
                 '<w:moveFrom w:id="3"><w:r><w:t>移动来源</w:t></w:r></w:moveFrom>'
                 '<w:moveTo w:id="4"><w:r><w:t>移动目的</w:t></w:r></w:moveTo>'
                 '<w:commentRangeStart w:id="9"/><w:r><w:t>批注锚定正文</w:t><w:commentReference w:id="9"/></w:r>'
                 '<w:commentRangeEnd w:id="9"/></w:p>')
    before = (tmp_path / "source.docx").read_bytes()
    original_run = subprocess.run

    def run(args, **kwargs):
        if "--convert-to" in args:
            with zipfile.ZipFile(args[-1]) as z:
                assert z.read("word/media/image1.png") == b"unchanged-image"
                for name in ("document", "header1", "footer1", "footnotes", "endnotes"):
                    xml = z.read(f"word/{name}.xml").decode("utf-8")
                    assert "旧预算" not in xml and "移动来源" not in xml and "right" not in xml
                    assert "新预算 42 dB" in xml and "移动目的" in xml and "批注锚定正文" in xml
                    assert 'w:val="left"' in xml and "commentReference" not in xml
                    assert "xmlns:w14=" in xml and 'mc:Ignorable="w14"' in xml
                assert "历史批注" not in z.read("word/comments.xml").decode("utf-8")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert (tmp_path / "source.docx").read_bytes() == before
    assert result.options["word_view"] == office.WORD_POLICY
    assert all(n["current_fact"] is False and n["source_xpath"] for n in result.notes)
    assert any("旧预算 12 dB" in n["text"] for n in result.notes)
    comment = next(n for n in result.notes if n["kind"] == "word_comment")
    assert comment["text"] == "历史批注" and len(comment["anchors"]) == 10
    assert load_office_materials(tmp_path, result.to_dict(), expected_manifest_hash=result.manifest_hash) == result
    with pytest.raises(OfficeMaterialError, match="位置不一致"):
        verify_office_materials(tmp_path, replace(result, notes=()))


def test_deleted_rows_cells_paragraph_marks_and_property_history(tmp_path):
    revised_word(tmp_path, '<w:p><w:pPr><w:rPr><w:del/></w:rPr></w:pPr><w:r><w:t>A</w:t></w:r></w:p>'
                 '<w:p><w:r><w:t>B</w:t></w:r></w:p>'
                 '<w:tbl><w:tblPr><w:tblPrExChange><w:tblPrEx/></w:tblPrExChange></w:tblPr>'
                 '<w:tr><w:trPr><w:del/></w:trPr><w:tc><w:p><w:r><w:t>旧行</w:t></w:r></w:p></w:tc></w:tr>'
                 '<w:tr><w:trPr><w:ins/></w:trPr><w:tc><w:tcPr><w:cellDel/></w:tcPr><w:p/></w:tc>'
                 '<w:tc><w:tcPr><w:cellIns/></w:tcPr><w:p><w:r><w:t>新行</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    data, _ = office._word_final_view((tmp_path / "source.docx").read_bytes())
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(data)) as z:
        tree = ET.fromstring(z.read("word/document.xml"))
        ns = {"w": office.W}
        assert len(tree.findall(".//w:tr", ns)) == 1
        assert len(tree.findall(".//w:tc", ns)) == 1
        assert len(tree.findall("w:body/w:p", ns)) == 1
        assert "".join(tree.itertext()) == "AB新行"
        assert b"tblPrExChange" not in z.read("word/document.xml")


@pytest.mark.parametrize("content", ['<w:tcPr><w:cellMerge/></w:tcPr>', '<w:p><w:pPr><w:rPr><w:del/></w:rPr></w:pPr></w:p>'])
def test_unsupported_structure_refuses_final_view(tmp_path, engine, content):
    revised_word(tmp_path, content)
    with pytest.raises(OfficeMaterialError) as error:
        prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    assert error.value.code == "invalid_package"
    assert not engine[1]


def test_legacy_identity_restore_does_not_reinterpret_or_replace(tmp_path, engine):
    revised_word(tmp_path, '<w:p><w:del w:id="1"><w:r><w:delText>12 dB</w:delText></w:r></w:del>'
                 '<w:ins w:id="2"><w:r><w:t>42 dB</w:t></w:r></w:ins></w:p>')
    current = prepare_office_materials(tmp_path, "source.docx", soffice=engine[0])
    options = dict(current.options)
    options.pop("word_view")
    identity = dict(preparation_version=office.LEGACY_VERSION, original_relpath=current.original_relpath,
                    original_hash=current.original_hash, source_format="docx", engine=current.engine, options=options)
    key = office._digest(identity)
    path = "office_materials/" + key + "/reading.pdf"
    legacy = replace(current, **identity, cache_key=key, material_relpath=path, notes=(), coverage_gaps=("旧版修订未验收",))
    folder = tmp_path / Path(path).parent
    folder.mkdir()
    shutil.copyfile(tmp_path / current.material_relpath, tmp_path / path)
    (folder / "manifest.json").write_text(json.dumps(legacy.to_dict()), encoding="utf-8")
    before = (folder / "manifest.json").read_bytes(), (tmp_path / path).read_bytes()
    restored = load_office_materials(tmp_path, legacy.to_dict(), expected_manifest_hash=legacy.manifest_hash)
    assert restored == legacy and restored.manifest_hash == legacy.manifest_hash
    assert prepare_office_materials(tmp_path, "source.docx", soffice=engine[0]) == current
    assert before == ((folder / "manifest.json").read_bytes(), (tmp_path / path).read_bytes())
    assert legacy.cache_key != current.cache_key
    with pytest.raises(OfficeMaterialError, match="身份与缓存键不一致"):
        verify_office_materials(tmp_path, replace(current, options=options))
    with pytest.raises(OfficeMaterialError, match="准备版本"):
        verify_office_materials(tmp_path, replace(current, preparation_version="future"))
