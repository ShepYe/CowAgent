"""安装版离线验收：在指定隔离目录验证真实冻结依赖和数据闭环。"""
import hashlib
import json
from pathlib import Path


def run(root):
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.wiki import WikiService
    from common.personal_tasks import TaskStore, OWNER
    from cli.commands.backup import create_backup_archive, restore_backup_archive
    from docx import Document
    from openpyxl import Workbook
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    (root / "config.json").write_text(json.dumps({"agent_workspace": str(workspace), "personal_assistant_enabled": True,
                                                  "embedding_enabled": False}), encoding="utf-8")
    samples = root / "samples"
    samples.mkdir(exist_ok=True)
    # A genuine one-page PDF without an authoring dependency in the runtime.
    stream = b"BT /F1 12 Tf 72 720 Td (PDF source budget 42 dB) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    content, offsets = b"%PDF-1.4\n", [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(content)
    content += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets[1:])
    content += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    (samples / "source.pdf").write_bytes(content)
    doc = Document()
    doc.add_heading("Source", 1)
    doc.add_paragraph("DOCX source budget 43 dB")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "Table evidence"
    doc.save(samples / "source.docx")
    book = Workbook()
    book.active.title = "Budget"
    book.active["B3"] = "XLSX source budget 44 dB"
    book.active["C3"] = "=1+1"
    book.save(samples / "source.xlsx")
    store = TaskStore(root / "personal")
    service = SourceService(workspace)
    results = []
    for path in sorted(samples.iterdir()):
        t = store.submit(OWNER, "selftest-" + path.suffix, {"text": "共享资料"}, {"channel": "selftest"}, "offline-selftest")
        attachment = store.attach(OWNER, t["task_id"], path, path.name, "file")
        if t["status"] == "queued":
            store.claim(OWNER, t["task_id"])
        s = service.receive(attachment["path"], filename=path.name, request_id=t["task_id"], origin=t["origin"])
        state = service.get(s["document_id"])
        if state["purpose"] != "long_term":
            service.change(s["document_id"], purpose="long_term", expected_state=state["state"], reason="离线验收")
        parsed = service.parse(s["revision_id"])
        wiki = WikiService(workspace)
        ordinal = next(i for i, unit in enumerate(parsed["units"]) if "budget" in unit["text"])
        candidate = wiki.prepare([dict(title=path.suffix[1:].upper() + " 离线预算知识", category="验收",
            blocks=[dict(content="## 预算依据\n" + parsed["units"][ordinal]["text"], sources=[dict(
                document_id=s["document_id"], revision_id=s["revision_id"], parse_id=parsed["parse_id"], ordinal=ordinal)])])],
            task_id=t["task_id"])
        with store.commit_guard(OWNER, t["task_id"], t["attempt"]):
            published = wiki.publish(candidate["batch_id"])[0]
            store.complete(OWNER, t["task_id"], t["attempt"], "published", [published])
        hit = service.read_reference(published["publish_id"], 0)
        assert hit["valid"] and service.search("budget")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == attachment["sha256"]
        results.append({"format": path.suffix, "location": hit["location"], "task_id": t["task_id"], "publish_id": published["publish_id"]})
    manifest = create_backup_archive(root / "snapshot.zip", root, workspace, complete=True)
    restored = root / "restored"
    # Restore entrypoint signatures stay governed by the existing CLI contract.
    restore_backup_archive(root / "snapshot.zip", restored, restored / "workspace")
    restored_tasks = TaskStore(restored / "personal")
    restored_sources = SourceService(restored / "workspace")
    assert len(restored_tasks.list()) == 3
    for item in results:
        attachment = restored_tasks.attachment(OWNER, item["task_id"], "file")
        assert Path(attachment["path"]).is_relative_to(restored)
        reference = restored_sources.read_reference(item["publish_id"], 0)
        assert reference["valid"] and Path(reference["original"]).is_relative_to(restored)
    assert len(restored_sources.search("budget")) == 3
    report = {"status": "passed", "formats": results, "backup_version": manifest["version"], "root": str(root), "index_mode": "keyword",
              "scope": "离线冻结依赖、原件、解析、关键词发布、出处、任务与备份恢复；未验证真实模型/飞书认证"}
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
