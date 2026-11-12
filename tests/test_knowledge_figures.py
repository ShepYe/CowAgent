"""受控图片的真实凭据、区域、迁移与异根恢复。"""

import copy
import os
from pathlib import Path
import sqlite3
import pytest

from agent.knowledge.figures import resolve_figure, validate_manifest
from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.reading import ReadingService
from agent.knowledge.wiki import WikiService
from tests.test_reading_ingest_flow import deliver, submission, flow as reading_flow

flow = reading_flow


def draft(flow, *, region=None, caption="原图"):
    root, store, task, entry, read, _ = flow
    options = dict(path=entry["path"], pages="1")
    if region is not None:
        options["region"] = region
    media = read.execute(options)
    assert media.status == "success", media.result
    deliver(media, task)
    evidence = submission(task, media.result)["reading_evidence"]
    if region is not None:
        evidence[0]["location"] = media.result["source_locations"][0]["location"]
    refs = ReadingService(root, task_store=store).resolve_evidence(task, "figures-only", evidence)
    source = next(iter(refs.values()))
    return dict(
        title="图片主题",
        summary="验证真实图文材料。",
        category="测试",
        blocks=[
            dict(
                content="可靠正文",
                sources=[source],
                figures=[
                    dict(figure_key=" image ", caption=caption, source=source, **({"region": region} if region else {}))
                ],
            )
        ],
    )


def publish(flow, page):
    wiki = WikiService(flow[0])
    batch = wiki.prepare([page])
    ref = wiki.publish(batch["batch_id"])[0]
    manifest = batch["pages"][0]["figure_manifest"]
    return wiki, ref, manifest["items"][0]


def test_ready_identity_and_corruption_leave_text(flow):
    wiki, ref, item = publish(flow, draft(flow))
    result = resolve_figure(wiki.workspace, ref["publish_id"], item["figure_id"])
    assert result["mime_type"] == "image/png"
    assert result["location"]["physical_page"] == 1 and result["source_index"] == 0
    Path(result["path"]).write_bytes(b"broken")
    assert wiki.current(ref["page_id"])
    with pytest.raises(SourceError) as error:
        resolve_figure(wiki.workspace, ref["publish_id"], item["figure_id"])
    assert error.value.code == "storage_failed"
    assert wiki.version(ref["publish_id"])["figure_state"] == "ready"


@pytest.mark.parametrize("region", [None, [0, 0, 0.6, 0.5], [True, 0, 0.5, 0.5], [0, 0, float("nan"), 1]])
def test_region_cannot_exceed_visual_evidence(flow, region):
    page = draft(flow, region=[0, 0, 0.5, 0.5])
    figure = page["blocks"][0]["figures"][0]
    if region is None:
        figure.pop("region")
    else:
        figure["region"] = region
    with pytest.raises(SourceError):
        WikiService(flow[0]).prepare([page])
    with SourceService(flow[0]).transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_region_containment_and_normalized_key(flow):
    page = draft(flow, region=[0, 0, 0.5, 0.5])
    figure = page["blocks"][0]["figures"][0]
    figure["region"] = [0.1, 0.1, 0.4, 0.4]
    wiki, ref, item = publish(flow, page)
    assert item["figure_key"] == "image"
    assert resolve_figure(wiki.workspace, ref["publish_id"], item["figure_id"])["data"].startswith(b"\x89PNG")
    page["blocks"][0]["figures"].append(dict(figure, figure_key="image"))
    with pytest.raises(SourceError, match="重复"):
        wiki.prepare([page])


@pytest.mark.parametrize(
    "caption", ["![偷图](https://invalid/image)", "[链接](https://invalid)", "<img src=x>", "公式 *强调*"]
)
def test_caption_stays_plain_markdown(flow, caption):
    wiki, ref, _ = publish(flow, draft(flow, caption=caption))
    body = wiki.version(ref["publish_id"])["content"]
    assert caption not in body
    assert body.count("](knowledge-figure:") == 1


@pytest.mark.parametrize(
    "field", ["original_hash", "reading_material_hash", "reading_manifest_hash", "reading_id", "source_page"]
)
def test_prepared_candidate_rejects_changed_material_identity(flow, field):
    wiki = WikiService(flow[0])
    batch = wiki.prepare([draft(flow)])
    record = copy.deepcopy(batch["pages"][0])
    record["figure_manifest"]["items"][0][field] = "forged"
    with wiki.source.transaction() as db, pytest.raises(SourceError):
        validate_manifest(db, wiki.source, record, check_bytes=True)


def test_startup_point_allows_first_write_with_live_pid(flow, monkeypatch):
    from common.figure_recovery import prepare_startup_recovery

    root = flow[0]
    page = draft(flow)
    # 模拟桌面父进程提前登记本次后端 PID，渠道及 worker 尚未创建。
    pid = root.parent / ".personal-backend.pid"
    pid.write_text(str(os.getpid()), encoding="utf-8")
    result = prepare_startup_recovery(root.parent, root)
    assert Path(result["archive"]).is_file()
    from cli.commands.backup import create_backup_archive

    with pytest.raises(ValueError, match="运行"):
        create_backup_archive(root.parent / "blocked.zip", root.parent, root, complete=True)
    wiki, ref, item = publish(flow, page)
    assert resolve_figure(root, ref["publish_id"], item["figure_id"])
    with wiki.source.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    ReadingService(root, task_store=flow[1])
    with SourceService(root).transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    pid.unlink()


def test_full_backup_relocates_current_and_history_without_old_root(flow, tmp_path):
    from cli.commands.backup import create_backup_archive, restore_backup_archive

    root = flow[0]
    wiki, ref, item = publish(flow, draft(flow))
    old = resolve_figure(root, ref["publish_id"], item["figure_id"])["data"]
    batch = wiki.prepare_figure_update(ref["page_id"], ref["publish_id"], task_id="retry")
    current = wiki.publish(batch["batch_id"])[0]
    new_item = batch["pages"][0]["figure_manifest"]["items"][0]
    archive = tmp_path / "full-images.zip"
    create_backup_archive(archive, root.parent, root, complete=True)
    target = tmp_path / "relocated"
    restore_backup_archive(archive, target, target / "workspace")
    # 原根重命名，使原绝对路径真实不可访问。
    hidden = root.parent.with_name(root.parent.name + "-offline")
    root.parent.rename(hidden)
    restored = target / "workspace"
    assert resolve_figure(restored, ref["publish_id"], item["figure_id"], "history")["data"] == old
    assert resolve_figure(restored, current["publish_id"], new_item["figure_id"])["data"] == old
    assert WikiService(restored).search("原图")


def test_empty_contract_v1_restart_without_reading_tables(tmp_path):
    from agent.knowledge.figures import prepare_manifest

    workspace = tmp_path / "workspace"
    service = SourceService(workspace)
    with service.transaction() as db:
        assert prepare_manifest(db, service, [dict(content="无图", source_ordinals=[], figures=[])], []) == dict(
            schema_version=1, items=[]
        )
    with SourceService(workspace).transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    ReadingService(workspace, task_store=None)
    with SourceService(workspace).transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("version", [4, 99])
def test_unknown_source_version_rejected_without_change(tmp_path, version):
    source = SourceService(tmp_path)
    with sqlite3.connect(source.db) as db:
        db.execute("PRAGMA user_version=" + str(version))
    with pytest.raises(SourceError) as error:
        SourceService(tmp_path)
    assert error.value.code == "version_conflict"
    with sqlite3.connect(source.db) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version


def test_single_image_read_does_not_touch_other_png(flow, monkeypatch):
    page = draft(flow)
    first = page["blocks"][0]["figures"][0]
    first["region"] = [0, 0, 0.5, 0.5]
    page["blocks"][0]["figures"].append(dict(first, figure_key="second", region=[0.5, 0.5, 1, 1]))
    wiki = WikiService(flow[0])
    batch = wiki.prepare([page])
    ref = wiki.publish(batch["batch_id"])[0]
    a, b = batch["pages"][0]["figure_manifest"]["items"]
    assert a["png_hash"] != b["png_hash"] or a["storage_path"] == b["storage_path"]
    # 使用不同长宽区域保证两份 PNG 具有不同字节。
    if a["storage_path"] == b["storage_path"]:
        page["page_id"] = ref["page_id"]
        page["expected_version"] = ref["publish_id"]
        page["blocks"][0]["figures"][1]["region"] = [0, 0, 0.7, 0.9]
        batch = wiki.prepare([page])
        ref = wiki.publish(batch["batch_id"])[0]
        a, b = batch["pages"][0]["figure_manifest"]["items"]
    other = wiki.workspace / b["storage_path"]
    other.write_bytes(b"corrupt")
    original = Path.read_bytes

    def guarded(path):
        if path.resolve() == other.resolve():
            pytest.fail("single figure read touched unrelated PNG")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    assert resolve_figure(wiki.workspace, ref["publish_id"], a["figure_id"])["data"]


def test_regeneration_repairs_same_hash_without_rewriting_history(flow):
    wiki, ref, item = publish(flow, draft(flow))
    result = resolve_figure(wiki.workspace, ref["publish_id"], item["figure_id"])
    immutable = wiki.version(ref["publish_id"])["figure_manifest"]
    Path(result["path"]).write_bytes(b"corrupt")
    batch = wiki.prepare_figure_update(ref["page_id"], ref["publish_id"], task_id="repair")
    updated = wiki.publish(batch["batch_id"])[0]
    repaired = resolve_figure(wiki.workspace, ref["publish_id"], item["figure_id"], "history")
    assert repaired["data"] == result["data"]
    assert wiki.version(ref["publish_id"])["figure_manifest"] == immutable
    assert updated["publish_id"] != ref["publish_id"]
