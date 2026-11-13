from fs_helpers import symlink_or_skip

from agent.knowledge.service import KnowledgeService

def test_directory_cycle_does_not_break_knowledge_listing(tmp_path):
    root = tmp_path / "knowledge"
    (root / "notes").mkdir(parents=True)
    (root / "notes/doc.md").write_text("# Local document")
    symlink_or_skip(root / "notes/loop", root, target_is_directory=True)
    svc = KnowledgeService(str(tmp_path))
    tree = svc.list_tree()
    assert tree["stats"]["pages"] == 1
    assert tree["tree"][0]["children"] == []

def test_external_symlink_titles_are_not_exposed(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    external = tmp_path / "outside"
    external.mkdir()
    (external / "secret.md").write_text("# Private title")
    symlink_or_skip(root / "external", external, target_is_directory=True)
    symlink_or_skip(root / "secret.md", external / "secret.md")
    tree = KnowledgeService(str(tmp_path)).list_tree()
    assert tree["tree"] == []
    assert tree["root_files"] == []
