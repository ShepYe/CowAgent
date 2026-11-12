"""工单16 / P-03 离线、有界证据探针；不是生产阅读凭据实现。

只在 create() 新建的临时子根中运行，模型响应与 Office 映射均为受信夹具。
运行：.venv/Scripts/python.exe -m scripts.document_reading_evidence_probe
"""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService, _hash, _json
from agent.knowledge.wiki import WikiService
from common.atomic_write import write_text_atomic


META = {"parse_id", "revision_id", "document_id", "sha256"}
FIELDS = {"reading_id", "evidence_key", "segment_id", "location", "observation", "type"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fail(code="invalid_request"):
    raise SourceError(code, "隔离探针拒绝无效或不可信证据")


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        fail()
    return value


class EvidenceProbe:
    """受信夹具 API 与模型输入 submit 分离；本地 SQLite 串行提交模拟屏障。"""

    @classmethod
    def create(cls, parent):
        root = Path(parent) / ("evidence-probe-" + uuid4().hex)
        root.mkdir()  # 不接管已有数据根
        (root / ".probe-only").write_text("P-03-v1", encoding="utf-8")
        return cls(root)

    def __init__(self, root):
        self.root = Path(root).resolve()
        if (self.root / ".probe-only").read_text(encoding="utf-8") != "P-03-v1":
            fail("forbidden")
        self.source = SourceService(self.root)
        self.wiki = WikiService(self.root)
        with self.source.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS probe_readings(id TEXT PRIMARY KEY,record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS probe_submissions(scope TEXT PRIMARY KEY,payload TEXT,refs TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS probe_heads(revision TEXT PRIMARY KEY,parse TEXT)")

    def _relative(self, relative):
        path = (self.root / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(self.root):
            fail("forbidden")
        return path

    def fixture_reading(self, *, office=False, outcome="completed", task="task-16", attempt="attempt-1"):
        """生成真实文件；成功回执由离线请求替身提供，不声称模型实际阅读。"""
        from PIL import Image, ImageDraw
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, NumberObject, DecodedStreamObject

        fixture = self.root / ("fixture-" + uuid4().hex)
        fixture.mkdir()
        image = Image.new("RGB", (100, 80), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 40, 60), fill="black")
        png = fixture / "graphic.png"
        image.save(png)
        writer = PdfWriter()
        page = writer.add_blank_page(100, 80)
        stream = DecodedStreamObject()
        stream.set_data(image.tobytes())
        stream.update({NameObject("/Type"): NameObject("/XObject"),
                       NameObject("/Subtype"): NameObject("/Image"),
                       NameObject("/Width"): NumberObject(100), NameObject("/Height"): NumberObject(80),
                       NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                       NameObject("/BitsPerComponent"): NumberObject(8)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject(
            {NameObject("/Im"): writer._add_object(stream)})})
        content = DecodedStreamObject()
        content.set_data(b"q 100 0 0 80 0 0 cm /Im Do Q")
        page[NameObject("/Contents")] = writer._add_object(content)
        pdf = fixture / "reading.pdf"
        writer.write(pdf)
        assert not PdfReader(pdf).pages[0].extract_text()
        original = pdf
        location = dict(kind="pdf", physical_page=1)
        if office:
            from docx import Document
            original = fixture / "graphic.docx"
            document = Document()
            document.add_picture(str(png))
            document.save(original)
            # 映射是夹具声明；没有使用或验证 Office 转换器。
            location = dict(kind="docx", part="word/media/image1.png", object="inline-1",
                            reading_copy_page=1)
        receipt = self.source.receive(original, filename=original.name, request_id=uuid4().hex,
                                      origin={"channel": "offline-evidence-probe"})
        self.source.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="隔离夹具")
        base = self.source.parse(receipt["revision_id"])
        reading = dict(schema_version=1, reading_id=uuid4().hex, owner=self.source.owner, task=task,
                       attempt=attempt, config_revision="offline-fixture-v1", outcome=outcome,
                       response_id="offline-response-" + uuid4().hex if outcome == "completed" else None,
                       document_id=receipt["document_id"], revision_id=receipt["revision_id"],
                       original=".personal_sources/originals/" + receipt["sha256"], sha256=receipt["sha256"],
                       copy=str(pdf.relative_to(self.root)), copy_sha256=_hash(pdf.read_bytes()),
                       converter="fixture-mapping-v1" if office else "identity-pdf-v1",
                       base_parse_id=base["parse_id"], segment_id=uuid4().hex, locations=[location])
        with self.source.transaction() as db:
            db.execute("INSERT INTO probe_readings VALUES(?,?)", (reading["reading_id"], canonical(reading)))
        return reading

    @staticmethod
    def observation(reading, key="figure", text="页面左侧有黑色竖直矩形"):
        return dict(reading_id=reading["reading_id"], evidence_key=key, segment_id=reading["segment_id"],
                    location=copy.deepcopy(reading["locations"][0]), observation=text, type="visual_interpretation")

    def _parse(self, parse_id, reading):
        parsed = json.loads((self.source.root / "parses" / (parse_id + ".json")).read_text(encoding="utf-8"))
        payload = {k: v for k, v in parsed.items() if k not in META}
        if (parsed.get("parse_id") != parse_id or parsed.get("document_id") != reading["document_id"]
                or parsed.get("revision_id") != reading["revision_id"] or parsed.get("sha256") != reading["sha256"]
                or _hash(_json([reading["sha256"], payload]).encode()) != parse_id):
            fail("storage_failed")
        return payload

    def submit(self, submission_id, evidence, *, owner="local-owner", task="task-16", attempt="attempt-1"):
        """模型仅可提交 FIELDS；owner/task/attempt 必须由受信运行时提供。"""
        identifier(submission_id)
        if not isinstance(evidence, list) or not evidence or len(evidence) > 100:
            fail()
        scope = canonical([owner, task, submission_id])
        with self.source.transaction() as db:
            normalized, seen = [], set()
            for item in evidence:
                if not isinstance(item, dict) or set(item) != FIELDS:
                    fail()
                for key in ("reading_id", "evidence_key", "segment_id", "observation"):
                    identifier(item[key])
                if item["type"] not in ("ocr", "visual_interpretation"):
                    fail()
                if item["observation"].strip() in ("已读取该文件", "附件已读", "已读取"):
                    fail()
                alias = (item["reading_id"], item["evidence_key"])
                if alias in seen:
                    fail()
                seen.add(alias)
                row = db.execute("SELECT record FROM probe_readings WHERE id=?", (item["reading_id"],)).fetchone()
                if not row:
                    fail()
                reading = json.loads(row[0])
                if reading.get("schema_version") != 1:
                    fail("version_conflict")
                if (reading["owner"], reading["task"], reading["attempt"]) != (owner, task, attempt):
                    fail("forbidden")
                if reading["outcome"] != "completed" or not reading["response_id"]:
                    fail("reading_incomplete")
                if (item["segment_id"] != reading["segment_id"]
                        or canonical(item["location"]) not in [canonical(loc) for loc in reading["locations"]]):
                    fail("invalid_request")
                for field, digest in (("original", "sha256"), ("copy", "copy_sha256")):
                    if _hash(self._relative(reading[field]).read_bytes()) != reading[digest]:
                        fail("storage_failed")
                doc = self.source._document(db, reading["document_id"])
                latest = db.execute("SELECT id FROM revisions WHERE document=? ORDER BY rowid DESC LIMIT 1",
                                    (reading["document_id"],)).fetchone()
                if doc["purpose"] != "long_term" or doc["validity"] != "active" or latest[0] != reading["revision_id"]:
                    fail("version_conflict")
                self._parse(reading["base_parse_id"], reading)
                normalized.append(dict(evidence=copy.deepcopy(item), material=reading))
            normalized.sort(key=lambda n: (n["evidence"]["reading_id"], n["evidence"]["evidence_key"]))
            payload = canonical(normalized)  # 全载荷含位置、观察、类型及程序绑定材料；不是只比别名
            old = db.execute("SELECT payload,refs FROM probe_submissions WHERE scope=?", (scope,)).fetchone()
            if old:
                if old[0] != payload:
                    fail("idempotency_conflict")
                refs = json.loads(old[1])
                for n, ref in zip(normalized, refs):
                    self._parse(ref["parse_id"], n["material"])
                return refs
            # 全部验证和冲突检查先于任何新解析文件／别名副作用。
            refs = []
            for n in normalized:
                item, reading = n["evidence"], n["material"]
                row = db.execute("SELECT parse FROM probe_heads WHERE revision=?", (reading["revision_id"],)).fetchone()
                parsed = self._parse(row[0] if row else reading["base_parse_id"], reading)
                ordinal = len(parsed["units"])
                parsed["units"].append(dict(text=item["observation"], type=item["type"],
                    location=dict(item["location"], type=item["type"]), origin="model",
                    probe_reading_provenance=dict(reading_id=reading["reading_id"], segment_id=reading["segment_id"],
                                            material_hash=_hash(canonical(reading).encode()))))
                parse_id = _hash(_json([reading["sha256"], parsed]).encode())
                parsed.update(parse_id=parse_id, document_id=reading["document_id"],
                              revision_id=reading["revision_id"], sha256=reading["sha256"])
                target = self.source.root / "parses" / (parse_id + ".json")
                if target.exists():
                    if json.loads(target.read_text(encoding="utf-8")) != parsed:
                        fail("storage_failed")
                else:
                    write_text_atomic(target, _json(parsed), newline="")
                db.execute("INSERT INTO probe_heads VALUES(?,?) ON CONFLICT(revision) DO UPDATE SET parse=excluded.parse",
                           (reading["revision_id"], parse_id))
                refs.append(dict(document_id=reading["document_id"], revision_id=reading["revision_id"],
                                 parse_id=parse_id, ordinal=ordinal))
            db.execute("INSERT INTO probe_submissions VALUES(?,?,?)", (scope, payload, canonical(refs)))
            return refs

    def resolve(self, sources, evidence, submission_id, **identity):
        """两种来源形状互斥；别名必须引用同一提交，不接受任意 parse_id。"""
        if not isinstance(sources, list) or not sources:
            fail()
        aliases = {(e["reading_id"], e["evidence_key"]) for e in evidence}
        for source in sources:
            if set(source) == {"reading_id", "evidence_key"}:
                if (source["reading_id"], source["evidence_key"]) not in aliases:
                    fail()
            elif set(source) != {"document_id", "revision_id", "parse_id", "ordinal"}:
                fail()
        refs = self.submit(submission_id, evidence, **identity) if evidence else []
        keys = sorted(aliases)
        mapping = dict(zip(keys, refs))
        return [mapping[(s["reading_id"], s["evidence_key"])] if "reading_id" in s else s for s in sources]

    def publish(self, refs):
        # 保留既有 prepare/publish 的来源、索引和提交前复核，不替换其守卫。
        pages = [dict(title="隔离图形主题", summary="记录夹具图形的可定位观察。", category="探针",
                      blocks=[dict(content="图形观察：页面左侧有黑色竖直矩形。", sources=refs)])]
        batch = self.wiki.prepare(pages, task_id="probe-publication")
        return self.wiki.publish(batch["batch_id"])


def main():
    with TemporaryDirectory(prefix="cowagent-p03-") as parent:
        probe = EvidenceProbe.create(parent)
        reading = probe.fixture_reading()
        refs = probe.submit("submission-1", [probe.observation(reading)])
        publication = probe.publish(refs)[0]
        readback = probe.wiki.read_reference(publication["publish_id"])
        assert readback["text"] == "页面左侧有黑色竖直矩形"
        print(json.dumps(dict(result="bounded_fixture_pass", source_type="model/visual_interpretation",
                              ordinal=refs[0]["ordinal"], real_model=False, real_office_conversion=False),
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
