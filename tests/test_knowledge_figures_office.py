"""Office 图文清单使用真实原件/阅读副本位置并可异根恢复。"""

import pytest
import pypdfium2  # noqa: F401 - initialize renderer before Office subprocess stub
from agent.knowledge.figures import resolve_figure
from agent.knowledge.wiki import WikiService
from agent.knowledge.reading_materials import PdfBudget
from tests.test_office_reading_flow import setup, prepare, complete, observation, env as office_env

office_environment = office_env


@pytest.fixture
def env(office_environment):
    from common.personal_tasks import TaskStore, OWNER

    workspace, _, task, executable, calls = office_environment
    store = TaskStore(workspace.parent / "personal")
    task = store.submit(OWNER, "office-images", {}, {"channel": "test"}, task["config_revision"])
    return workspace, store, task, executable, calls


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
def test_office_figure_mapping_hash_and_relocated_backup(env, suffix, tmp_path):
    from cli.commands.backup import create_backup_archive, restore_backup_archive

    workspace, store, _, _, _ = env
    task, receipts = setup(env, (suffix,))
    task, parsed, materials, registry, _ = prepare(env, task, receipts[0], budget=PdfBudget(max_pages=1))
    record = registry.prepare(task, receipts[0], parsed, materials)
    complete(registry, task, record)
    refs = registry.resolve_evidence(task, "office-images", [observation(record, materials.location(1))])
    ref = next(iter(refs.values()))
    wiki = WikiService(workspace)
    batch = wiki.prepare(
        [
            dict(
                title="Office 图片",
                summary="验证原件与副本图片映射。",
                category="测试",
                blocks=[
                    dict(
                        content="流程图",
                        sources=[ref],
                        figures=[dict(figure_key="流程", caption="业务流程图", source=ref)],
                    )
                ],
            )
        ]
    )
    pub = wiki.publish(batch["batch_id"])[0]
    item = batch["pages"][0]["figure_manifest"]["items"][0]
    assert item["location"] == materials.location(1)
    assert item["original_hash"] == receipts[0]["sha256"]
    assert item["reading_material_hash"] == materials.original_hash != item["original_hash"]
    assert item["reading_manifest_hash"] == materials.manifest_hash
    before = resolve_figure(workspace, pub["publish_id"], item["figure_id"])
    if suffix == "docx":
        assert before["location"] == dict(kind="word_reading_copy_page", reading_copy_page=1)
    else:
        assert before["location"] == dict(kind="slide", source_slide_id="901", source_slide_number=1)
    archive = tmp_path / "office-images.zip"
    create_backup_archive(archive, workspace.parent, workspace, complete=True)
    target = tmp_path / "restored"
    restore_backup_archive(archive, target, target / "workspace")
    workspace.rename(workspace.with_name("offline-old-root"))
    after = resolve_figure(target / "workspace", pub["publish_id"], item["figure_id"])
    assert after["data"] == before["data"] and after["item"] == before["item"]
