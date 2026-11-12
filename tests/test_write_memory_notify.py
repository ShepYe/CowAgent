from pathlib import Path
from agent.tools.write.write import Write

def test_protected_memory_write_is_rejected_before_notification(tmp_path):
    class Config:
        def get_workspace(self):
            return Path(tmp_path)
    class Memory:
        config = Config()
        def mark_dirty(self):
            raise AssertionError("rejected write must not notify")
    result = Write({"cwd": str(tmp_path), "memory_manager": Memory()}).execute({"path": "MEMORY.md", "content": "saved fact"})
    assert not (tmp_path / "MEMORY.md").exists()
    assert result.status == "error", result.result
    assert "memory_record" in str(result.result)

def test_nonmemory_write_does_not_notify(tmp_path):
    class Config:
        def get_workspace(self):
            return Path(tmp_path)
    class Memory:
        config = Config()
        def mark_dirty(self):
            raise AssertionError("unexpected notification")
    result = Write({"cwd": str(tmp_path), "memory_manager": Memory()}).execute({"path": "notes.txt", "content": "saved"})
    assert result.status == "success"
