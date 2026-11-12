"""P-03 真实来源／Wiki 守卫的隔离契约实验；无网络和真实库。"""
import copy
import json
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.knowledge.parser import SourceError
from scripts.document_reading_evidence_probe import EvidenceProbe


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("探针禁止出网"))
    return EvidenceProbe.create(tmp_path)


def counts(probe):
    with probe.source.transaction() as db:
        return (db.execute("SELECT count(*) FROM probe_submissions").fetchone()[0],
                db.execute("SELECT count(*) FROM wiki_publications").fetchone()[0],
                len(list((probe.source.root / "parses").glob("*.json"))))


@pytest.mark.parametrize("office", [False, True])
def test_image_only_to_topic_and_typed_readback(probe, office):
    reading = probe.fixture_reading(office=office)
    base_path = probe.source.root / "parses" / (reading["base_parse_id"] + ".json")
    original_base = base_path.read_bytes()
    base = json.loads(original_base)
    assert base["units"] == []
    observation = probe.observation(reading)
    refs = probe.resolve([dict(reading_id=reading["reading_id"], evidence_key="figure")],
                         [observation], "submit-1")
    publication = probe.publish(refs)[0]
    result = probe.wiki.read_reference(publication["publish_id"])
    assert result["text"] == observation["observation"]
    assert result["location"] == dict(observation["location"], type="visual_interpretation")
    parsed = json.loads((probe.source.root / "parses" / (refs[0]["parse_id"] + ".json")).read_text(encoding="utf-8"))
    unit = parsed["units"][refs[0]["ordinal"]]
    assert unit["type"] == "visual_interpretation" and unit["origin"] == "model"
    assert parsed["units"][:len(base["units"])] == base["units"]
    assert base_path.read_bytes() == original_base
    assert probe.wiki.search("图形观察")
    assert "type" not in result and "origin" not in result  # 当前 Wiki 公共回读的边界


@pytest.mark.parametrize("outcome", ["prepared", "sent", "unknown", "cancelled", "over_budget", "unreadable"])
def test_no_success_receipt_no_side_effect(probe, outcome):
    reading = probe.fixture_reading(outcome=outcome)
    before = counts(probe)
    with pytest.raises(SourceError, match="隔离探针") as error:
        probe.submit("s", [probe.observation(reading)])
    assert error.value.code == "reading_incomplete"
    assert counts(probe) == before


@pytest.mark.parametrize("mutation", ["page", "segment", "owner", "task", "attempt", "placeholder", "type", "injected"])
def test_invalid_evidence_rejected_before_writes(probe, mutation):
    reading = probe.fixture_reading()
    item = probe.observation(reading)
    identity = {}
    if mutation == "page":
        item["location"]["physical_page"] = 2
    elif mutation == "segment":
        item["segment_id"] = "other"
    elif mutation in ("owner", "task", "attempt"):
        identity[mutation] = "other"
    elif mutation == "placeholder":
        item["observation"] = "已读取该文件"
    elif mutation == "type":
        item["type"] = "text"
    else:
        item["sha256"] = reading["sha256"]
    before = counts(probe)
    with pytest.raises(SourceError):
        probe.submit("s", [item], **identity)
    assert counts(probe) == before


@pytest.mark.parametrize("field", ["original", "copy", "parse"])
def test_tampered_files_rejected(probe, field):
    reading = probe.fixture_reading()
    target = (probe.source.root / "parses" / (reading["base_parse_id"] + ".json")
              if field == "parse" else probe.root / reading[field])
    if field == "parse":
        parsed = json.loads(target.read_text(encoding="utf-8"))
        parsed["units"] = [dict(text="伪造", location={"physical_page": 1})]
        target.write_text(json.dumps(parsed), encoding="utf-8")
    else:
        target.write_bytes(target.read_bytes() + b"tamper")
    before = counts(probe)
    with pytest.raises(SourceError) as error:
        probe.submit("s", [probe.observation(reading)])
    assert error.value.code == "storage_failed"
    assert counts(probe) == before


def test_full_payload_order_restart_append_and_conflict(probe):
    readings = [probe.fixture_reading(), probe.fixture_reading(office=True)]
    items = [probe.observation(r) for r in readings]  # 多 reading 复用 figure 别名
    refs = probe.submit("s", items)
    before = counts(probe)
    reopened = EvidenceProbe(probe.root)  # 模拟观察已存、候选未存的重启
    reordered = [dict(reversed(list(item.items()))) for item in reversed(items)]
    assert reopened.submit("s", reordered) == refs
    assert counts(probe) == before
    for field, value in (("observation", "不同的观察"), ("type", "ocr")):
        changed = copy.deepcopy(items)
        changed[0][field] = value
        with pytest.raises(SourceError) as error:
            reopened.submit("s", changed)
        assert error.value.code == "idempotency_conflict"
        assert counts(probe) == before
    first = probe.observation(readings[0], key="extra", text="矩形位于页面左侧")
    appended = probe.submit("s2", [first])[0]
    old_ref = next(r for r in refs if r["revision_id"] == appended["revision_id"])
    assert appended["ordinal"] == old_ref["ordinal"] + 1
    assert appended["parse_id"] != old_ref["parse_id"]
    # 旧解析、旧出处仍可发布；新观察只追加，不覆盖。
    reopened.publish(refs)


def test_duplicate_key_and_mixed_source_shape(probe):
    reading = probe.fixture_reading()
    item = probe.observation(reading)
    before = counts(probe)
    with pytest.raises(SourceError):
        probe.submit("s", [item, item])
    with pytest.raises(SourceError):
        probe.resolve([dict(reading_id=reading["reading_id"], evidence_key="figure", parse_id="injected")], [item], "s")
    assert counts(probe) == before


def test_concurrent_replays_only_allocate_once(probe):
    reading = probe.fixture_reading()
    item = probe.observation(reading)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: probe.submit("s", [item]), range(3)))
    assert results[0] == results[1] == results[2]
    assert counts(probe) == (1, 0, 2)


def test_existing_publish_guard_rechecks_parse_and_original(probe):
    reading = probe.fixture_reading()
    refs = probe.submit("s", [probe.observation(reading)])
    bad_ordinal = dict(refs[0], ordinal=999)
    with pytest.raises(SourceError):
        probe.publish([bad_ordinal])
    # prepare 与 publish 间篡改原件，验证提交前真实守卫而非探针自证。
    page = dict(title="候选", summary="候选摘要", category="探针",
                blocks=[dict(content="有据的结论", sources=refs)])
    batch = probe.wiki.prepare([page])
    path = probe.root / reading["original"]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(SourceError):
        probe.wiki.publish(batch["batch_id"])
    assert counts(probe)[1] == 0


def test_unknown_record_version_and_source_withdrawal(probe):
    reading = probe.fixture_reading()
    with probe.source.transaction() as db:
        changed = dict(reading, schema_version=999)
        db.execute("UPDATE probe_readings SET record=? WHERE id=?", (json.dumps(changed), reading["reading_id"]))
    with pytest.raises(SourceError) as error:
        probe.submit("s", [probe.observation(reading)])
    assert error.value.code == "version_conflict"
    reading = probe.fixture_reading()
    probe.source.change(reading["document_id"], action="withdraw", expected_state=1, reason="撤回夹具")
    with pytest.raises(SourceError):
        probe.submit("s", [probe.observation(reading)])
