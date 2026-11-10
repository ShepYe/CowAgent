"""Source startup must notice missing and outdated requirements before launching."""
import sys
from importlib import metadata

from scripts.check_personal_deps import dependency_problems


def test_missing_scheduler_dependency_is_reported(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests>=2.28.2\ncroniter>=2.0.0\n", encoding="utf-8")

    def installed_version(name):
        if name == "croniter":
            raise metadata.PackageNotFoundError(name)
        return "2.32.3"

    monkeypatch.setattr(metadata, "version", installed_version)
    assert dependency_problems([requirements]) == ["croniter>=2.0.0: missing"]


def test_versions_and_environment_markers_across_both_lists(tmp_path, monkeypatch):
    core = tmp_path / "requirements.txt"
    desktop = tmp_path / "requirements-desktop.txt"
    core.write_text("aiohttp>=3.8.6,<3.10; python_version < '3.13'\n", encoding="utf-8")
    desktop.write_text(
        "# Desktop constraints\n--only-binary=cryptography\n"
        "aiohttp>=3.10; python_version >= '3.13'\n"
        "playwright==1.52.0 # Browser driver\n"
        "absent-on-windows; sys_platform == 'missing-platform'\n",
        encoding="utf-8-sig",
    )
    versions = {"aiohttp": "3.10.0" if sys.version_info >= (3, 13) else "3.9.5", "playwright": "1.51.0"}
    monkeypatch.setattr(metadata, "version", versions.__getitem__)
    assert dependency_problems([core, desktop]) == ["playwright==1.52.0: installed 1.51.0"]


def test_ready_environment_and_duplicate_requirements(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("croniter>=2.0.0\ncroniter>=2.0.0\n", encoding="utf-8")
    monkeypatch.setattr(metadata, "version", lambda name: "2.0.7")
    assert dependency_problems([requirements]) == []
    monkeypatch.setattr(metadata, "version", lambda name: "1.0.0")
    assert dependency_problems([requirements]) == ["croniter>=2.0.0: installed 1.0.0"]
