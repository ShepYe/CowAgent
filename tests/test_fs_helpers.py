"""能力检查只跳过指定错误，不掩盖错误路径或文件系统故障。"""

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from fs_helpers import symlink_or_skip


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP, "win1314"])
def test_symlink_capability_errors_skip_current_case(tmp_path, code):
    error = OSError(errno.EACCES if code == "win1314" else code, "unavailable")
    if code == "win1314":
        error.winerror = 1314
    with patch.object(Path, "symlink_to", side_effect=error):
        with pytest.raises(pytest.skip.Exception):
            symlink_or_skip(tmp_path / "link", tmp_path / "target")


@pytest.mark.parametrize("code", [errno.ENOENT, errno.EEXIST, errno.EIO, errno.EACCES])
def test_other_symlink_errors_are_propagated(tmp_path, code):
    error = OSError(code, "unexpected filesystem error")
    with patch.object(Path, "symlink_to", side_effect=error):
        with pytest.raises(OSError) as exc:
            symlink_or_skip(tmp_path / "link", tmp_path / "target")
    assert exc.value is error


@pytest.mark.parametrize("directory", [False, True])
def test_success_returns_link_and_preserves_directory_flag(tmp_path, directory):
    link, target = tmp_path / "link", tmp_path / "target"
    with patch.object(Path, "symlink_to") as create:
        assert symlink_or_skip(link, target, target_is_directory=directory) == link
    create.assert_called_once_with(target, target_is_directory=directory)


def test_created_link_resolves_to_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("linked content", encoding="utf-8")
    link = symlink_or_skip(tmp_path / "link.txt", target)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()
    assert link.read_text(encoding="utf-8") == "linked content"
