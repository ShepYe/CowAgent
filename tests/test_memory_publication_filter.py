"""Publication filtering must not couple ordinary memory search to registry IO."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.memory.manager import MemoryManager
from agent.memory.storage import SearchResult


def manager_with(hits, workspace):
    for item in hits:
        path = item.path.replace("\\", "/")
        if path.startswith("memory/"):
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.snippet, encoding="utf-8")
    manager = MemoryManager.__new__(MemoryManager)
    manager.config = SimpleNamespace(get_workspace=lambda: workspace, max_results=10, min_score=0, sync_on_search=False,
                                     vector_weight=0.7, keyword_weight=0.3)
    manager.embedding_provider = None
    manager.reranker = None
    manager.storage = SimpleNamespace(search_keyword=lambda **kw: list(hits))
    return manager


def hit(path):
    return SearchResult(path=path, start_line=1, end_line=1, score=1,
                        snippet="budget", source="knowledge" if path.startswith("knowledge") else "memory")


@pytest.mark.parametrize("paths", [[], ["memory/shared/note.md"]])
def test_memory_only_search_does_not_resolve_knowledge(paths, monkeypatch, tmp_path):
    from common import state_dir
    resolve = Mock(side_effect=AssertionError("memory search consulted registry"))
    monkeypatch.setattr(state_dir, "knowledge_dir", resolve)
    manager = manager_with([hit(path) for path in paths], tmp_path)
    assert [r.path for r in asyncio.run(manager.search("budget"))] == paths
    resolve.assert_not_called()


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_knowledge_filter_resolves_once_and_keeps_memory(tmp_path, monkeypatch, separator):
    from common import state_dir
    from agent.knowledge import personal_assistant
    paths = ["memory/shared/note.md", "knowledge/managed/stale.md", "knowledge/notes/current.md"]
    manager = manager_with([hit(path.replace("/", separator)) for path in paths], tmp_path)
    manager.config.get_workspace = lambda: tmp_path
    resolve = Mock(return_value=tmp_path / "knowledge")
    monkeypatch.setattr(state_dir, "knowledge_dir", resolve)
    checked = []
    def readable(path):
        checked.append(path)
        return path.name != "stale.md"
    monkeypatch.setattr(personal_assistant, "readable", readable)
    results = asyncio.run(manager.search("budget"))
    assert [r.path.replace("\\", "/") for r in results] == [paths[0], paths[2]]
    assert checked == [tmp_path / "knowledge/managed/stale.md", tmp_path / "knowledge/notes/current.md"]
    resolve.assert_called_once_with(base=tmp_path)


def test_below_threshold_knowledge_does_not_resolve_root(monkeypatch, tmp_path):
    from common import state_dir
    monkeypatch.setattr(state_dir, "knowledge_dir", Mock(side_effect=AssertionError("below cutoff")))
    manager = manager_with([hit("knowledge/managed/stale.md")], tmp_path)
    assert asyncio.run(manager.search("budget", min_score=1.1)) == []


@pytest.mark.parametrize("user,shared,expected", [(None, True, True), ("local-owner", True, True),
                                                   ("local-owner", False, False), ("other", True, False)])
def test_publication_bridge_scope_and_withdrawal(tmp_path, monkeypatch, user, shared, expected):
    from agent.knowledge.personal_assistant import SourceService
    from agent.memory.config import MemoryConfig
    from common import state_dir
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda **kw: tmp_path / "knowledge")
    service = SourceService(tmp_path)
    path = tmp_path / "input.txt"
    path.write_text("budget 42", encoding="utf-8")
    source = service.receive(path, filename=path.name, request_id="test", origin={"test": 1})
    service.change(source["document_id"], purpose="long_term", reason="reference", expected_state=0)
    service.publish(service.prepare(source["revision_id"])["candidate_id"])
    manager = MemoryManager(MemoryConfig(workspace_root=str(tmp_path)), embedding_provider=None)
    try:
        results = asyncio.run(manager.search("budget", user_id=user, include_shared=shared, min_score=0))
        assert bool(results) == expected
        if expected:
            assert "source_reference" in results[0].snippet
        service.change(source["document_id"], action="withdraw", expected_state=1, reason="withdraw")
        assert asyncio.run(manager.search("budget", user_id=user, include_shared=shared, min_score=0)) == []
    finally:
        manager.close()
