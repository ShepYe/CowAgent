"""真实依赖、自测入口隔离、可选转换器缺失及冻结清单契约。"""
import ast
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from common.document_reading_selftest import main, run


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("文档依赖自测不得出网")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_real_dependencies_and_missing_converter_are_independent(tmp_path, monkeypatch):
    cwd = Path.cwd()
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path / "real-data"))
    root = tmp_path / "isolated"
    report = run(root, soffice=str(tmp_path / "missing-soffice.exe"))
    assert report["status"] == "partial"
    assert [c["status"] for c in report["checks"]] == ["passed"] * 5 + ["unavailable"]
    assert report["checks"][-1]["affects_pdf"] is False
    assert report["checks"][-1]["code"] == "engine_unavailable"
    assert report["cloud_api_called"] is False
    assert json.loads((root / "result.json").read_text(encoding="utf-8")) == report
    assert not (tmp_path / "real-data").exists()
    assert Path.cwd() == cwd and os.environ["COW_DATA_DIR"] == str(tmp_path / "real-data")
    assert (root / report["checks"][1]["details"]["png"]).is_file()
    assert all(c["details"]["original_hash_unchanged"] for c in report["checks"][:5])


def test_missing_pdfium_does_not_hide_other_checks(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    report = run(tmp_path / "missing-pdfium")
    checks = {c["name"]: c for c in report["checks"]}
    assert report["status"] == "failed"
    assert checks["pdfium_region"]["status"] == "failed"
    assert checks["pdfium_region"]["code"] == "renderer_unavailable"
    for name in ("pdf", "word_structure", "ppt_structure", "xlsx_embedded_image"):
        assert checks[name]["status"] == "passed"


@pytest.mark.parametrize("existing", ["directory", "file"])
def test_existing_root_is_rejected_without_changes(tmp_path, existing):
    root = tmp_path / "existing"
    if existing == "directory":
        root.mkdir()
        sentinel = root / "config.json"
    else:
        sentinel = root
    sentinel.write_text("真实配置保留", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run(root)
    assert sentinel.read_text(encoding="utf-8") == "真实配置保留"
    if root.is_dir():
        assert list(root.iterdir()) == [sentinel]


def test_cli_failure_exit_code(tmp_path, capsys):
    assert main(["--document-reading-self-test", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "FileExistsError"


def test_relative_converter_is_resolved_before_chdir(tmp_path, monkeypatch):
    from common import document_reading_selftest as selftest
    monkeypatch.chdir(tmp_path)
    executable = tmp_path / "soffice.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(selftest.shutil, "which", lambda _: "soffice.exe")
    seen = []
    def convert(root, suffix, soffice):
        seen.append(soffice)
        assert Path(soffice).is_absolute() and Path(soffice).is_file()
        return {}
    monkeypatch.setattr(selftest, "_convert", convert)
    report = run(tmp_path / "relative-converter", soffice="soffice.exe")
    assert report["status"] == "passed" and seen == [str(executable)] * 2


def test_cli_bypasses_business_imports_and_real_data(tmp_path):
    real = tmp_path / "real-data"
    real.mkdir()
    (real / "config.json").write_text('{"sentinel":"真实配置"}', encoding="utf-8")
    before = (real / "config.json").read_bytes()
    root = tmp_path / "cli-root"
    # 若 app.py 的显式分支移到业务 import 后面，这个独立进程会立即失败。
    script = """
import importlib.abc, runpy, sys
class BlockBusiness(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'config', 'channel', 'plugins', 'models'}:
            raise RuntimeError('自测触发业务导入: ' + fullname)
sys.meta_path.insert(0, BlockBusiness())
app, root = sys.argv[1:]
sys.path.insert(0, str(__import__('pathlib').Path(app).parent))
sys.argv = [app, '--document-reading-self-test', root]
runpy.run_path(app, run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT / "app.py"), str(root)],
        cwd=tmp_path, env=dict(os.environ, COW_DATA_DIR=str(real), PYTHONUTF8="1"),
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "partial" and report["runtime"]["frozen"] is False
    assert (real / "config.json").read_bytes() == before
    assert list(real.iterdir()) == [real / "config.json"]
    assert {p.name for p in tmp_path.iterdir()} == {"real-data", "cli-root"}


def test_packaging_static_dependencies_cover_reading_modules():
    spec = ast.parse((ROOT / "desktop/build/cowagent-backend.spec").read_text(encoding="utf-8"))
    collections = {(node.func.id, node.args[0].value) for node in ast.walk(spec)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"collect_submodules", "collect_dynamic_libs", "collect_data_files", "copy_metadata"}
        and node.args and isinstance(node.args[0], ast.Constant)}
    assert {("collect_submodules", package) for package in (
        "agent.knowledge", "common", "pypdf", "docx", "pptx", "openpyxl", "pypdfium2", "pypdfium2_raw")} <= collections
    assert ("collect_dynamic_libs", "pypdfium2_raw") in collections
    assert ("copy_metadata", "pypdfium2") in collections
    assert {("collect_data_files", package) for package in ("docx", "pptx", "pypdfium2_raw")} <= collections
    literals = {node.value for node in ast.walk(spec) if isinstance(node, ast.Constant)
                and isinstance(node.value, str)}
    assert {"PIL.Image", "PIL.PngImagePlugin"} <= literals
    requirements = (ROOT / "desktop/build/requirements-desktop.txt").read_text(encoding="utf-8")
    for package in ("pypdf", "pypdfium2", "python-docx", "python-pptx", "openpyxl", "Pillow"):
        assert any(line.strip().startswith(package) for line in requirements.splitlines())
