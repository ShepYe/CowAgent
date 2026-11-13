"""文件系统测试的局部能力检查。"""

import errno
from pathlib import Path

import pytest


def symlink_or_skip(link, target, target_is_directory=False):
    """实际创建链接；仅在平台不支持或进程缺少权限时跳过当前用例。"""
    link = Path(link)
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314 or exc.errno in {
            errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP,
        }:
            pytest.skip(f"当前文件系统或进程无法创建符号链接：{exc}")
        raise
    return link
