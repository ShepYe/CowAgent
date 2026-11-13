"""File tools neither wait on FIFOs nor replace them with regular files."""

import multiprocessing
import os
import stat
from types import SimpleNamespace

from fs_helpers import symlink_or_skip

import pytest

from agent.tools.edit.edit import Edit
from agent.tools.memory.memory_get import MemoryGetTool
from agent.tools.write.write import Write
from common.atomic_write import write_text_atomic


@pytest.fixture
def pipe(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("POSIX FIFO fixture")
    path = tmp_path / "memory" / "pipe.txt"
    path.parent.mkdir()
    os.mkfifo(path)
    return path


@pytest.mark.parametrize("name", ["edit", "memory_get"])
@pytest.mark.parametrize("alias", [False, True])
def test_reading_tools_refuse_a_fifo_without_a_writer(pipe, name, alias):
    target = pipe
    if alias:
        target = pipe.with_name("alias.txt")
        symlink_or_skip(target, pipe)
    # Fork retains the actual imported repository modules. The bounded worker
    # makes a blocking regression observable without stranding the test suite.
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()

    def run():
        if name == "edit":
            result = Edit({"cwd": str(pipe.parent)}).execute({
                "path": str(target), "oldText": "old", "newText": "new",
            })
        else:
            manager = SimpleNamespace(config=SimpleNamespace(get_workspace=lambda: pipe.parent.parent))
            result = MemoryGetTool(manager).execute({"path": str(target)})
        result_queue.put((result.status, result.result))

    process = context.Process(target=run)
    process.start()
    try:
        process.join(timeout=2)
        assert not process.is_alive(), "file tool waited for a FIFO writer"
        assert process.exitcode == 0
        status, message = result_queue.get(timeout=1)
        assert status == "error"
        assert "regular file" in message
        assert stat.S_ISFIFO(pipe.stat().st_mode)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        result_queue.close()
        result_queue.join_thread()


@pytest.mark.parametrize("alias", [False, True])
def test_write_preserves_existing_fifo_instead_of_replacing_it(pipe, alias):
    target = pipe
    if alias:
        target = pipe.with_name("alias.txt")
        symlink_or_skip(target, pipe)
    result = Write({"cwd": str(pipe.parent)}).execute({"path": str(target), "content": "new text\n"})
    assert result.status == "error"
    assert "regular file" in result.result
    assert stat.S_ISFIFO(pipe.stat().st_mode)
    if alias:
        assert target.is_symlink()


def test_atomic_writer_refuses_special_targets_before_staging(pipe):
    with pytest.raises(OSError, match="regular file"):
        write_text_atomic(pipe, "new text")
    assert stat.S_ISFIFO(pipe.stat().st_mode)
    assert list(pipe.parent.glob(".*.tmp")) == []


@pytest.mark.parametrize("alias", [False, True])
def test_regular_symlink_targets_keep_the_existing_contract(tmp_path, alias):
    memory = tmp_path / "files"
    memory.mkdir()
    source = memory / "note.txt"
    source.write_text("old text\n", encoding="utf-8")
    target = source
    if alias:
        target = symlink_or_skip(memory / "alias.txt", source)
    assert Edit({"cwd": str(tmp_path)}).execute({"path": str(target), "oldText": "old", "newText": "edited"}).status == "success"
    assert target.is_symlink() == alias
    assert source.read_text(encoding="utf-8") == "edited text\n"
    assert Write({"cwd": str(tmp_path)}).execute({"path": str(target), "content": "new text\n"}).status == "success"
    assert source.read_text(encoding="utf-8") == "new text\n"


@pytest.mark.parametrize("alias", [False, True])
def test_memory_get_reads_regular_targets(tmp_path, alias):
    memory = tmp_path / "memory"
    memory.mkdir()
    source = memory / "note.txt"
    source.write_text("memory text\n", encoding="utf-8")
    target = symlink_or_skip(memory / "alias.txt", source) if alias else source
    manager = SimpleNamespace(config=SimpleNamespace(get_workspace=lambda: tmp_path))
    result = MemoryGetTool(manager).execute({"path": target.name})
    assert result.status == "success"
    assert "memory text" in result.result


def test_atomic_writer_creates_new_regular_files(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    write_text_atomic(memory / "created.txt", "created\n")
    assert (memory / "created.txt").read_text() == "created\n"
