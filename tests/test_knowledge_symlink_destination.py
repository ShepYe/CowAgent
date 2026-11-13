from fs_helpers import symlink_or_skip

import pytest
from agent.knowledge.service import KnowledgeService

def service(tmp_path, monkeypatch):
    (tmp_path / "knowledge" / "notes").mkdir(parents=True)
    svc = KnowledgeService(str(tmp_path))
    monkeypatch.setattr(svc, "_sync_index", lambda *a, **k: None)
    return svc

def test_import_cannot_overwrite_external_symlink_target(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    external = tmp_path / "private.md"
    external.write_text("private original")
    symlink_or_skip(tmp_path / "knowledge/notes/doc.md", external)
    result = svc.import_documents("notes", [{"filename": "doc.md", "content": "replacement"}], "overwrite")
    assert result["failed"] == 1 and result["imported"] == 0
    assert external.read_text() == "private original"

def test_create_rejects_dangling_external_symlink(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    external = tmp_path / "not-created.md"
    symlink_or_skip(tmp_path / "knowledge/notes/doc.md", external)
    with pytest.raises(ValueError, match="outside knowledge"):
        svc.create_document("notes/doc.md", "replacement")
    assert not external.exists()

@pytest.mark.parametrize("alias", [False, True])
def test_regular_import_and_internal_symlink_remain_supported(tmp_path, monkeypatch, alias):
    svc = service(tmp_path, monkeypatch)
    real = tmp_path / ("knowledge/notes/real.md" if alias else "knowledge/notes/doc.md")
    real.write_text("old")
    if alias:
        symlink_or_skip(tmp_path / "knowledge/notes/doc.md", real)
    result = svc.import_documents("notes", [{"filename": "doc.md", "content": "new"}], "overwrite")
    assert result["imported"] == 1
    assert real.read_text() == "new"
