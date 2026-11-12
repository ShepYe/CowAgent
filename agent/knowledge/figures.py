"""知识页的受控 PNG、不可变清单与发布关联。"""

import copy
from dataclasses import replace
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import unicodedata
from uuid import uuid4

from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import _hash

SOURCE_KEYS = ("document_id", "revision_id", "parse_id", "ordinal")
TABLES = {
    "figure_assets": {"id", "record"},
    "figure_publications": {"publication", "block_ordinal", "figure_key", "record"},
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fail(message, code="invalid_request"):
    raise SourceError(code, message)


def validate_schema(db):
    for table, columns in TABLES.items():
        if not columns.issubset({r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}):
            fail("图片资产结构不完整", "version_conflict")
        for row in db.execute("SELECT record FROM " + table):
            item = json.loads(row[0])
            if type(item.get("schema_version")) is not int or item["schema_version"] != 1:
                fail("未知图片记录版本", "version_conflict")
    for table in ("wiki_batches", "wiki_publications"):
        for row in db.execute("SELECT record FROM " + table):
            record = json.loads(row[0])
            for page in record.get("pages", [record]):
                if "figure_manifest" in page:
                    _manifest(page["figure_manifest"])


def prepare_recovery_point(service, data_root=None):
    """停止后端后调用；完整恢复点不触发任何数据库升级。"""
    from common.figure_recovery import startup_recovery

    existing = startup_recovery(service.workspace)
    if existing is not None:
        return existing
    from cli.commands.backup import create_backup_archive

    if data_root is None:
        candidates = [service.workspace, service.workspace.parent]
        data_root = next((p for p in candidates if (p / "config.json").is_file()), service.workspace.parent)
    target = service.root / "recovery" / ("pre-figures-" + uuid4().hex + ".zip")
    return create_backup_archive(target, Path(data_root), service.workspace, complete=True)


def ensure_schema(db, service):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 3:
        validate_schema(db)
        return
    if version not in (1, 2):
        fail("未知来源库版本", "version_conflict")
    # 来源事务已阻止其他来源写入；备份入口同时要求实例停止。
    prepare_recovery_point(service)
    db.execute("CREATE TABLE figure_assets(id TEXT PRIMARY KEY,record TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE figure_publications(publication TEXT NOT NULL,block_ordinal INTEGER NOT NULL,"
        "figure_key TEXT NOT NULL,record TEXT NOT NULL,PRIMARY KEY(publication,block_ordinal,figure_key))"
    )
    db.execute("PRAGMA user_version=3")


def _text(value, limit):
    if not isinstance(value, str):
        fail("图片键及图注须为纯文本")
    value = value.strip()
    if not 1 <= len(value) <= limit or any(
        unicodedata.category(c).startswith("C") or c in "\r\n\u2028\u2029" for c in value
    ):
        fail("图片键或图注超限或包含控制字符")
    return value


def _region(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
    ):
        fail("图片区域须为四个有限数字")
    a, b, c, d = value
    if not (0 <= a < c <= 1 and 0 <= b < d <= 1):
        fail("图片区域须在页内且具有正面积")
    return [float(v) for v in value]


def _inputs(blocks, sources):
    result, seen = [], set()
    for ordinal, block in enumerate(blocks):
        figures = block.get("figures")
        if not isinstance(figures, list):
            fail("图文页每块须显式提供 figures 数组")
        for figure in figures:
            if not isinstance(figure, dict) or set(figure) - {"figure_key", "caption", "source", "region"}:
                fail("图片输入包含未知字段")
            key, caption = _text(figure.get("figure_key"), 128), _text(figure.get("caption"), 512)
            if key in seen:
                fail("图片键规范化后重复")
            seen.add(key)
            source = figure.get("source")
            if (
                not isinstance(source, dict)
                or set(source) != set(SOURCE_KEYS)
                or type(source.get("ordinal")) is not int
            ):
                fail("图片须使用已解析的四元出处")
            index = next(
                (
                    i
                    for i in block["source_ordinals"]
                    if type(i) is int and 0 <= i < len(sources) and all(sources[i][k] == source[k] for k in SOURCE_KEYS)
                ),
                None,
            )
            if index is None:
                fail("图片出处与正文块不一致")
            result.append(
                dict(
                    block_ordinal=ordinal,
                    figure_key=key,
                    caption=caption,
                    source=copy.deepcopy(source),
                    source_index=index,
                    region=_region(figure.get("region", [0, 0, 1, 1])),
                )
            )
    return result


def _evidence(db, service, item, sources, *, verify):
    from agent.knowledge.reading import verify_reading_unit, _read
    from agent.knowledge.reading_materials import load_pdf_materials

    snap = sources[item["source_index"]]
    parsed = json.loads((service.root / "parses" / (snap["parse_id"] + ".json")).read_text(encoding="utf-8"))
    unit = parsed["units"][snap["ordinal"]]
    if unit.get("type") != "visual_interpretation" or "reading_provenance" not in unit:
        fail("图片须有已交付的具体视觉观察", "reading_incomplete")
    if verify:
        metadata = verify_reading_unit(db, service, unit, snap)
        if any(snap.get(k) != v for k, v in metadata.items()):
            fail("视觉观察快照不符", "storage_failed")
    record = _read(db, unit["reading_provenance"]["reading_id"])
    material = load_pdf_materials(service.workspace, record["manifest"], expected_manifest_hash=record["manifest_hash"])
    location = {k: v for k, v in unit["location"].items() if k != "type"}
    pages = [p for p in material.target_pages if material.location(p) == location]
    if len(pages) != 1:
        fail("图片必须对应单一真实源页")
    region = item["region"]
    bounds = _region(location.get("region", [0, 0, 1, 1]))
    if not (bounds[0] <= region[0] < region[2] <= bounds[2] and bounds[1] <= region[1] < region[3] <= bounds[3]):
        fail("请求图片超出可信视觉观察范围", "reading_incomplete")
    revision = json.loads(
        db.execute(
            "SELECT record FROM revisions WHERE id=? AND document=?", (snap["revision_id"], snap["document_id"])
        ).fetchone()[0]
    )
    return (
        material,
        pages[0],
        dict(
            location=location,
            original_hash=revision["sha256"],
            reading_material_hash=material.original_hash,
            reading_manifest_hash=material.manifest_hash,
            reading_id=record["reading_id"],
            source_page=pages[0],
        ),
    )


def _path(service, item):
    digest = item.get("png_hash", "")
    if (
        not re.fullmatch("[0-9a-f]{64}", digest)
        or item.get("storage_path") != ".personal_sources/figures/" + digest + ".png"
    ):
        fail("图片相对存储身份不符", "storage_failed")
    path = (service.workspace / item["storage_path"]).resolve()
    if not path.is_relative_to((service.root / "figures").resolve()) or (service.root / "figures").is_symlink():
        fail("图片路径越界", "forbidden")
    return path


def _png(data):
    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as picture:
            if picture.format != "PNG" or picture.width * picture.height > 4_000_000:
                raise ValueError()
            picture.verify()
    except Exception as exc:
        raise SourceError("storage_failed", "图片不是有效的受控 PNG") from exc


def _bytes(service, item):
    path = _path(service, item)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SourceError("storage_failed", "派生图片缺失") from exc
    if len(data) > 8 * 1024 * 1024 or _hash(data) != item["png_hash"]:
        fail("派生图片哈希不符", "storage_failed")
    _png(data)
    return data, path


def _store(service, png):
    digest = _hash(png)
    item = dict(png_hash=digest, storage_path=".personal_sources/figures/" + digest + ".png")
    path = _path(service, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != png:
        temporary = path.with_name("." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(png)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    _bytes(service, item)
    return item


def prepare_manifest(db, service, blocks, sources):
    from agent.knowledge.reading_materials import _plan_region, ReadingMaterialError

    items = _inputs(blocks, sources)
    # 全部输入和凭据先验证；非法请求不能混入 pending 或触发迁移。
    evidence = [_evidence(db, service, item, sources, verify=True) for item in items]
    ensure_schema(db, service)
    output = []
    for item, (material, page, identity) in zip(items, evidence):
        item = dict(item, **identity, schema_version=1)
        try:
            main = replace(material, region_supplement=None)
            rendered, png, _ = _plan_region(
                service.workspace,
                main,
                page,
                item["region"],
                scale=3,
                max_pixels=4_000_000,
                max_png_bytes=8 * 1024 * 1024,
            )
            _png(png)
            item.update(
                _store(service, png),
                state="ready",
                figure_id=uuid4().hex,
                renderer_version=rendered.preparation_version,
                render_parameters=copy.deepcopy(rendered.region_supplement),
            )
            # 临时阅读缓存身份不能成为图片永久身份。
            item["render_parameters"].pop("png_relpath", None)
        except (OSError, ReadingMaterialError, SourceError) as exc:
            if getattr(exc, "code", "storage_failed") not in (
                "storage_failed",
                "render_failed",
                "renderer_unavailable",
                "budget_exceeded",
            ):
                raise
            item.update(state="pending", gap=dict(code=getattr(exc, "code", "storage_failed"), message=str(exc)))
        output.append(item)
    return dict(schema_version=1, items=output)


def _manifest(manifest):
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or set(manifest) != {"schema_version", "items"}
        or not isinstance(manifest["items"], list)
    ):
        fail("未知图片清单版本或结构", "version_conflict")
    return manifest["items"]


def _escape(value):
    return re.sub(r"([\\`*_{}\[\]()<>!#|])", r"\\\1", value)


def render_blocks(blocks, manifest, publish_id):
    items = _manifest(manifest)
    result = copy.deepcopy(blocks)
    for ordinal, block in enumerate(result):
        body = block.get("content_without_figures", block["content"])
        if "knowledge-figure:" in body.lower():
            fail("正文不能自写受控图片引用")
        block["content_without_figures"] = body
        parts = [body]
        for item in items:
            if item["block_ordinal"] != ordinal:
                continue
            caption = _escape(item["caption"])
            if item["state"] == "ready":
                parts.append(f"![{caption}](knowledge-figure:{publish_id}/{item['figure_id']})")
            else:
                parts.append(f"{caption}（待补图）〔出处 {item['source_index'] + 1}〕")
        block["content"] = "\n\n".join(parts)
    return result


def _validate_item_identity(db, service, record, item):
    _, _, identity = _evidence(db, service, item, record["sources"], verify=True)
    if any(item.get(k) != v for k, v in identity.items()):
        fail("image immutable material identity mismatch", "storage_failed")


def _validate_item_bytes(db, service, record, item):
    _validate_item_identity(db, service, record, item)
    return _bytes(service, item)


def validate_manifest(db, service, record, check_bytes=False):
    """候选验证载荷；已提交发布还须逐项验证数据库不可变关系。"""
    try:
        if "figure_manifest" not in record:
            if any("figures" in b for b in record["blocks"]):
                fail("图文记录缺少图片清单", "storage_failed")
            return
        items = _manifest(record["figure_manifest"])
        if db.execute("PRAGMA user_version").fetchone()[0] != 3:
            fail("图片契约要求来源库 v3", "version_conflict")
        expected = _inputs(record["blocks"], record["sources"])
        if len(expected) != len(items):
            fail("图片输入与清单不符", "storage_failed")
        ids = set()
        for item, base in zip(items, expected):
            if type(item.get("schema_version")) is not int or item["schema_version"] != 1:
                fail("未知图片项版本", "version_conflict")
            if any(item.get(k) != v for k, v in base.items()):
                fail("图片与正文出处关联不符", "storage_failed")
            snap = record["sources"][item["source_index"]]
            if item["location"] != {k: v for k, v in snap["location"].items() if k != "type"}:
                fail("图片源位置不符", "storage_failed")
            revision = json.loads(
                db.execute(
                    "SELECT record FROM revisions WHERE id=? AND document=?", (snap["revision_id"], snap["document_id"])
                ).fetchone()[0]
            )
            if (
                item["original_hash"] != revision["sha256"]
                or item["reading_id"] != snap.get("reading_id")
                or item["reading_manifest_hash"] != snap.get("reading_copy", {}).get("manifest_hash")
            ):
                fail("图片来源或阅读材料身份不符", "storage_failed")
            from agent.knowledge.reading import _read

            reading = _read(db, item["reading_id"])
            material = reading["manifest"]
            if (
                item["reading_material_hash"] != material["original_hash"]
                or type(item["source_page"]) is not int
                or item["source_page"] not in material["target_pages"]
            ):
                fail("image material identity mismatch", "storage_failed")
            page = item["source_page"]
            derived = material.get("derived_source")
            if derived is None:
                mapped = dict(kind="pdf", physical_page=page)
            else:
                mapped = derived["office_manifest"]["page_map"][page - 1]
                if mapped["kind"] == "word_reading_copy_page":
                    mapped = dict(kind=mapped["kind"], reading_copy_page=mapped["reading_copy_page"])
                else:
                    mapped = dict(
                        kind="slide",
                        source_slide_id=mapped["source_slide_id"],
                        source_slide_number=mapped["source_slide_number"],
                    )
            if mapped != {k: v for k, v in item["location"].items() if k != "region"}:
                fail("image source page mapping mismatch", "storage_failed")
            if check_bytes:
                _validate_item_identity(db, service, record, item)
            if item["state"] == "ready":
                fid = item["figure_id"]
                if not isinstance(fid, str) or not re.fullmatch("[0-9a-f]{32}", fid) or fid in ids:
                    fail("图片身份无效", "storage_failed")
                ids.add(fid)
                _path(service, item)
                if not item.get("renderer_version") or not isinstance(item.get("render_parameters"), dict):
                    fail("缺少固定渲染身份", "storage_failed")
                params = item["render_parameters"]
                if (
                    params.get("schema_version") != 1
                    or params.get("source_page") != item["source_page"]
                    or params.get("region") != item["region"]
                    or params.get("png_hash") != item["png_hash"]
                ):
                    fail("图片固定渲染参数不符", "storage_failed")
                if check_bytes:
                    _bytes(service, item)
            elif (
                item["state"] != "pending"
                or not item.get("gap")
                or any(k in item for k in ("png_hash", "storage_path", "figure_id"))
            ):
                fail("非法图片状态", "storage_failed")
        version = record["page_version"]
        if render_blocks(record["blocks"], record["figure_manifest"], version) != record["blocks"]:
            fail("图片正文投影不一致", "storage_failed")
        published = db.execute("SELECT 1 FROM wiki_publications WHERE id=?", (version,)).fetchone()
        if published:
            rows = db.execute(
                "SELECT record FROM figure_publications WHERE publication=? ORDER BY block_ordinal,figure_key",
                (version,),
            ).fetchall()
            if [json.loads(r[0]) for r in rows] != sorted(items, key=lambda i: (i["block_ordinal"], i["figure_key"])):
                fail("图片发布关联不符", "storage_failed")
            for item in items:
                if item["state"] == "ready":
                    row = db.execute("SELECT record FROM figure_assets WHERE id=?", (item["figure_id"],)).fetchone()
                    if not row or json.loads(row[0]) != item:
                        fail("图片资产关联不符", "storage_failed")
    except SourceError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
        raise SourceError("storage_failed", "图片清单损坏") from exc


def attach_publication(db, service, record):
    if "figure_manifest" not in record:
        return
    for item in _manifest(record["figure_manifest"]):
        if item["state"] == "ready":
            old = db.execute("SELECT record FROM figure_assets WHERE id=?", (item["figure_id"],)).fetchone()
            if old and json.loads(old[0]) != item:
                fail("不可变图片身份冲突", "storage_failed")
            db.execute("INSERT OR IGNORE INTO figure_assets VALUES(?,?)", (item["figure_id"], canonical(item)))
        db.execute(
            "INSERT INTO figure_publications VALUES(?,?,?,?)",
            (record["page_version"], item["block_ordinal"], item["figure_key"], canonical(item)),
        )


def resolve_figure(workspace, publish_id, figure_id, view="current"):
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.wiki import _valid
    from agent.knowledge.catalog import verify_archive

    if view not in ("current", "history") or any(
        not isinstance(v, str) or not re.fullmatch(r"[\w-]+", v) for v in (publish_id, figure_id)
    ):
        fail("图片请求身份或视图无效")
    service = SourceService(workspace)
    with service.transaction() as db:
        pub = db.execute("SELECT * FROM wiki_publications WHERE id=?", (publish_id,)).fetchone()
        if not pub:
            fail("图片发布不存在")
        if view == "current":
            if not _valid(db, service, pub):
                fail("图片不属于有效当前发布", "version_conflict")
            record = json.loads(pub["record"])
        else:
            record, _ = verify_archive(db, service, pub)
        items = _manifest(record.get("figure_manifest"))
        item = next((i for i in items if i.get("figure_id") == figure_id and i["state"] == "ready"), None)
        if item is None:
            fail("图片不属于该发布")
        data, path = _validate_item_bytes(db, service, record, item)
        return dict(
            data=data,
            mime_type="image/png",
            item=copy.deepcopy(item),
            path=str(path),
            location=copy.deepcopy(item["location"]),
            source_index=item["source_index"],
            source=copy.deepcopy(record["sources"][item["source_index"]]),
        )
