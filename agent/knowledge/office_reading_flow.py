"""Office 原生阅读的受信准备入口；转换与模型发送分离，不调用云端。

只支持 DOCX/PPTX；可见页可准备，转换 notes/coverage_gaps 原样保留。
调用方仍需决定格式放行并验证媒体能力。本模块不启用任何原生开关。
"""
import json
from pathlib import Path
import re

from pypdf import PdfReader

from agent.knowledge.office_materials import load_office_materials, prepare_office_materials
from agent.knowledge.parser import SourceError, parse_source
from agent.knowledge.personal_assistant import SourceService, _hash, _json
from agent.knowledge.reading_materials import (
    PdfBudget, _read, _relative, _store, bind_office_pdf, office_coverage_keys, prepare_pdf_materials,
)


def _current(store, task):
    current = store.get(task["owner_id"], task["task_id"])
    if current["config_revision"] != task["config_revision"]:
        raise SourceError("version_conflict", "任务配置已改变")
    return current


def _sources(store, task, source):
    decisions = task.get("attachment_decisions") or {}
    if set(decisions) != {a["attachment_id"] for a in task["attachments"]}:
        raise SourceError("invalid_request", "原生阅读前须登记全部附件用途")
    result = []
    for attachment in task["attachments"]:
        decision = decisions[attachment["attachment_id"]]
        if decision["purpose"] != "long_term":
            continue
        receipt = source.registered_revision(decision["source"]["document_id"], decision["source"]["revision_id"])
        entry = store.attachment(task["owner_id"], task["task_id"], attachment["attachment_id"])
        if receipt["sha256"] != entry["sha256"] or receipt["filename"] != entry["filename"]:
            raise SourceError("version_conflict", "阅读来源与本任务附件不符")
        with source.transaction() as db:
            doc = source._document(db, receipt["document_id"])
            latest = db.execute("SELECT id FROM revisions WHERE document=? ORDER BY rowid DESC LIMIT 1",
                                (receipt["document_id"],)).fetchone()
            if doc["purpose"] != "long_term" or doc["validity"] != "active" or latest[0] != receipt["revision_id"]:
                raise SourceError("version_conflict", "阅读来源已失效")
        relative = ".personal_sources/originals/" + receipt["sha256"]
        if _hash(_read(source.workspace, relative)) != receipt["sha256"]:
            raise SourceError("storage_failed", "Office 原件哈希不符")
        result.append(receipt)
    if not result:
        raise SourceError("invalid_request", "没有长期阅读目标")
    return result


def _office_copy(store, task, source, receipt, *, soffice, timeout_seconds):
    suffix = Path(receipt["filename"]).suffix.lower()
    if suffix not in (".docx", ".pptx"):
        raise SourceError("attachment_unsupported", "Office 原生阅读仅支持 DOCX/PPTX")
    digest = receipt["sha256"]
    original = ".personal_sources/originals/" + digest
    work = f"office_sources/{digest}/source{suffix}"
    anchor = (task.get("source_office_materials") or {}).get(receipt["revision_id"])
    if anchor is None:
        data = _read(source.workspace, original)
        if _hash(data) != digest:
            raise SourceError("storage_failed", "原件哈希不符")
        # 保留内容地址原件，仅为引擎提供同哈希、受控后缀的工作材料。
        _store(source.workspace, work, data)
        office = prepare_office_materials(source.workspace, work, soffice=soffice,
                                          timeout_seconds=timeout_seconds)
        with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
            current = _current(store, task)
            anchors = dict(current.get("source_office_materials") or {})
            # 并发准备始终采用先提交的排版，不覆盖已固定的任务材料。
            anchor = anchors.get(receipt["revision_id"])
            if anchor is None:
                anchor = dict(manifest=office.to_dict(), manifest_hash=office.manifest_hash)
                anchors[receipt["revision_id"]] = anchor
                store._change(task["owner_id"], task["task_id"], task["attempt"], {"running"},
                              source_office_materials=anchors)
    office = load_office_materials(source.workspace, anchor["manifest"],
                                  expected_manifest_hash=anchor["manifest_hash"])
    if office.original_relpath != work or office.original_hash != digest:
        raise SourceError("storage_failed", "固定 Office 清单与原件不符")
    return office


def prepare_office_task_target(store, task, workspace, *, soffice="soffice", timeout_seconds=120):
    """在接受局部页窗之前固定全部 PDF/Office 可见阅读目标。

    Office 缺口保留在 task.source_office_materials；目标仅表示本次可读范围，
    不声称隐藏页、备注图片等已完成。混合任务须由调用层统一使用此入口。
    """
    from agent.knowledge.reading_publication import register_target

    source = SourceService(workspace, task["owner_id"])
    with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
        current = _current(store, task)
        receipts = _sources(store, current, source)
    targets = set()
    for receipt in receipts:
        suffix = Path(receipt["filename"]).suffix.lower()
        if suffix == ".pdf":
            path = _relative(source.workspace, ".personal_sources/originals/" + receipt["sha256"])
            targets.update(f"{receipt['revision_id']}:{p}" for p in range(1, len(PdfReader(path).pages) + 1))
        else:
            office = _office_copy(store, current, source, receipt, soffice=soffice, timeout_seconds=timeout_seconds)
            targets.update(office_coverage_keys(receipt["revision_id"], office.to_dict()))
    # 转换期间的取消／来源变化不能把未授权材料登记为任务目标。
    with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
        latest = _current(store, task)
        if receipts != _sources(store, latest, source):
            raise SourceError("version_conflict", "转换期间阅读目标已改变")
        return register_target(store, latest, sorted(targets))


def _base_parse(store, task, source, receipt, office):
    """新准备稿保留原件结构；已有锚及其模型观察按原 ID 不可变接续。"""
    with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
        current = _current(store, task)
        anchors = dict(current.get("source_native_parses") or {})
        anchor = anchors.get(receipt["revision_id"])
        if anchor:
            # 不使用历史 revision.original 绝对路径；异根恢复仍按相对身份核验。
            if not isinstance(anchor, str) or not re.fullmatch(r"[0-9a-f]{64}", anchor):
                raise SourceError("storage_failed", "Office 解析锚无效")
            try:
                parsed = json.loads(_read(source.workspace, f".personal_sources/parses/{anchor}.json"))
                payload = {k: v for k, v in parsed.items() if k not in {
                    "parse_id", "document_id", "revision_id", "sha256"}}
                if (any(parsed.get(k) != receipt[k] for k in ("document_id", "revision_id", "sha256"))
                        or parsed.get("parse_id") != anchor
                        or payload.get("source_identity") != dict(document_id=receipt["document_id"],
                                                                  revision_id=receipt["revision_id"])
                        or _hash(_json([receipt["sha256"], payload]).encode()) != anchor):
                    raise ValueError("parse identity/hash")
                return parsed
            except (ValueError, TypeError, AttributeError) as exc:
                raise SourceError("storage_failed", "Office 解析稿身份或哈希不符") from exc
        relative = ".personal_sources/originals/" + receipt["sha256"]
        if _hash(_read(source.workspace, relative)) != receipt["sha256"]:
            raise SourceError("storage_failed", "Office 原件哈希不符")
        payload = parse_source(_relative(source.workspace, relative), receipt["filename"], prepare_only=True)
        if _hash(_read(source.workspace, relative)) != receipt["sha256"]:
            raise SourceError("storage_failed", "结构解析期间 Office 原件改变")
        source_parser = dict(parser_id=payload["parser_id"], parser_version=payload["parser_version"],
                             config=dict(payload["config"]))
        payload.update(parser_id="office-native-reading", parser_version="2",
            config=dict(payload["config"], preparation="native-office-v2", source_parser=source_parser,
                        office_manifest_hash=office.manifest_hash),
            source_identity=dict(document_id=receipt["document_id"], revision_id=receipt["revision_id"]))
        payload["visual_warnings"] = [*payload.get("visual_warnings", []),
            *[dict(kind="office_conversion", status="pending", reason=gap) for gap in office.coverage_gaps]]
        if any(note.get("kind") == "word_revision" for note in office.notes):
            # 原件结构 parser 尚未消解所有修订；不能把附加层文字补作当前正文。
            payload["visual_warnings"].append(dict(kind="word_revision_structure", status="pending",
                reason="source_structure_not_final_word_view", office_manifest_hash=office.manifest_hash))
        parse_id = _hash(_json([receipt["sha256"], payload]).encode())
        parsed = dict(payload, parse_id=parse_id, document_id=receipt["document_id"],
                      revision_id=receipt["revision_id"], sha256=receipt["sha256"])
        # 与 SourceService 同一不可变内容寻址协议，字节原子发布且不覆盖。
        _store(source.workspace, f".personal_sources/parses/{parse_id}.json", _json(parsed).encode("utf-8"))
        anchors[receipt["revision_id"]] = parse_id
        store._change(task["owner_id"], task["task_id"], task["attempt"], {"running"}, source_native_parses=anchors)
        return parsed


def prepare_office_read(store, task, workspace, receipt, model, first=1, last=None, budget=None,
                        *, soffice="soffice", timeout_seconds=120):
    """返回 (task, parsed, PreparedPdf, registry, capability_identity)，不发送媒体。

    first/last 是阅读 PDF 全局页窗，观察位置用 materials.location(page)。
    先固定全部附件目标，再选页；notes/gaps 在 derived_source.office_manifest。
    """
    from agent.knowledge.reading import ReadingService
    from agent.knowledge.reading_capabilities import capability_identity

    task = prepare_office_task_target(store, task, workspace, soffice=soffice, timeout_seconds=timeout_seconds)
    source = SourceService(workspace, task["owner_id"])
    receipts = _sources(store, task, source)
    registered = next((r for r in receipts if r["document_id"] == receipt["document_id"]
                       and r["revision_id"] == receipt["revision_id"]), None)
    if registered is None:
        raise SourceError("forbidden", "资料不属于本任务长期附件")
    office = _office_copy(store, task, source, registered, soffice=soffice, timeout_seconds=timeout_seconds)
    last = office.page_count if last is None else last
    if type(first) is not int or type(last) is not int or not 1 <= first <= last <= office.page_count:
        raise SourceError("invalid_request", "阅读副本页窗越界")
    parsed = _base_parse(store, task, source, registered, office)
    materials = prepare_pdf_materials(workspace, office.material_relpath,
        document_id=registered["document_id"], revision_id=registered["revision_id"], parse_id=parsed["parse_id"],
        config_revision=task["config_revision"], target_pages=range(first, last + 1),
        budget=PdfBudget() if budget is None else budget)
    materials = bind_office_pdf(workspace, materials, office,
        original_relpath=".personal_sources/originals/" + registered["sha256"], original_hash=registered["sha256"])
    registry = ReadingService(workspace, task_store=store)
    # 复核取消、来源和清单身份；调用方可再次 prepare，幂等返回同一 reading。
    registry.prepare(task, registered, parsed, materials)
    return task, parsed, materials, registry, capability_identity(model, task["config_revision"])
