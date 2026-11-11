"""P-01 确定性工具回路替身；执行证据存入 local/p01-evidence。
仅验证真实工具链，模型判断质量由真实模型调试单独验收。

启动：.venv/Scripts/python scripts/verify_personal_desktop.py serve
断言：.venv/Scripts/python scripts/verify_personal_desktop.py check
只接受虚构测试密钥，不转发任何网络请求，不记录 prompt 或密钥。
"""
import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parents[1] / "local/p01-evidence"
LOG = EVIDENCE / "requests.jsonl"


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict)
                         and p.get("type") in ("text", "input_text"))
    return ""


def _tool_value(raw):
    """解包真实工具回执；错误和非 JSON 正文保留给调用方验证。"""
    value = content_text(raw)
    for _ in range(3):
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except (ValueError, TypeError):
            break
        if isinstance(value, dict) and "result" in value:
            if value.get("status") == "error":
                return value
            value = value["result"]
        else:
            break
    return value


def choose_response(body):
    """确定性工具回路替身，仅验收执行链；模型判断质量由真实调试验收。"""
    messages = body.get("messages", [])
    user_index = next((i for i in range(len(messages) - 1, -1, -1)
                       if messages[i].get("role") == "user"
                       and content_text(messages[i].get("content"))), -1)
    text = content_text(messages[user_index].get("content")) if user_index >= 0 else ""
    tools = {t.get("function", {}).get("name") for t in body.get("tools", [])}
    calls = {c["id"]: c["function"] for m in messages[user_index + 1:] for c in m.get("tool_calls", [])}
    returned = []
    for message in messages[user_index + 1:]:
        call = calls.get(message.get("tool_call_id"))
        if message.get("role") == "tool" and call:
            returned.append((call["name"], json.loads(call.get("arguments", "{}")),
                             _tool_value(message.get("content"))))
    task = re.search(r"\[系统接收记录\]\s*task_id=([A-Za-z0-9_-]+)", text)
    stats = dict(tool_offered="ls" in tools, ingest_offered="knowledge_ingest" in tools,
                 search_offered="memory_search" in tools, tool_returned=len(returned),
                 tool_success=False, refs_count=0, skill_read=False, attachment_reads=0,
                 pages_submitted=False, fixture_kind="deterministic-tool-loop")

    def call(name, args):
        stats.update(tool_called=name, purpose=args.get("purpose"))
        if name not in tools:
            return {"content": "P01_TOOL_UNAVAILABLE " + name}, stats
        return {"tool_calls": [{"index": 0, "id": "call_p01_" + str(len(returned)), "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}, stats

    def finish(answer, success=False):
        stats.update(tool_called=None, tool_success=success)
        return {"content": answer}, stats

    if task:
        try:
            attachments = json.loads(text.split("附件：", 1)[1].strip())
        except (ValueError, IndexError):
            return finish("INGEST_UNVERIFIED missing attachment manifest")
        # 技能路径必须来自本轮真实系统技能清单，不猜本机路径。
        system = "\n".join(content_text(m.get("content")) for m in messages if m.get("role") == "system")
        locations = re.findall(r"<location>(.*?)</location>", system, flags=re.S)
        skill_path = next((p.strip() for p in locations if "knowledge-wiki" in p.replace("\\", "/")), None)
        if not skill_path:
            return finish("INGEST_UNVERIFIED knowledge-wiki not loaded")
        stats["skill_path"] = skill_path
        skill_reads = [v for n, a, v in returned if n == "read" and a.get("path") == skill_path]
        if not skill_reads:
            return call("read", {"path": skill_path})
        skill_value = skill_reads[-1]
        skill_text = skill_value.get("content", "") if isinstance(skill_value, dict) else str(skill_value)
        stats["skill_read"] = "knowledge_ingest" in skill_text and "pages" in skill_text
        if not stats["skill_read"]:
            return finish("INGEST_UNVERIFIED skill read failed")
        materials = []
        for entry in attachments:
            reads = [v for n, a, v in returned if n == "read" and a.get("path") == entry["path"]]
            if not reads:
                return call("read", {"path": entry["path"]})
            material = reads[-1]
            if not isinstance(material, dict) or not material.get("parse_id") or not material.get("units"):
                return finish("INGEST_UNVERIFIED attachment read failed")
            materials.append(material)
        stats["attachment_reads"] = len(materials)
        intent = text[:task.start()]
        purpose = "transient" if "P01_TRANSIENT" in intent else "waiting" if "P01_WAITING" in intent else "long_term"
        args = dict(task_id=task[1], purpose=purpose, reason="确定性工具回路：按测试标记分流" if purpose != "waiting" else "应替代哪份资料？")
        ingests = [(a, v) for n, a, v in returned if n == "knowledge_ingest"]
        if not ingests:
            return call("knowledge_ingest", args)
        submitted, receipt = ingests[-1]
        stats["pages_submitted"] = bool(submitted.get("pages"))
        stats["organization_required"] = bool(isinstance(receipt, dict) and receipt.get("organization_required"))
        if not isinstance(receipt, dict) or receipt.get("task_id") != task[1]:
            return finish("INGEST_UNVERIFIED " + json.dumps(receipt, ensure_ascii=False))
        if purpose == "long_term" and receipt.get("organization_required") and not stats["pages_submitted"]:
            sources, facts = [], []
            for material in materials:
                unit = material["units"][0]
                evidence = unit["text"]
                budget = re.search(r"(?:budget|预算).*?(\d+(?:\.\d+)?)\s*(dB)?", evidence, re.I)
                if not budget:
                    return finish("INGEST_UNVERIFIED fixture requires explicit budget evidence")
                facts.append("资料给出的链路 budget 为 " + budget[1] + (" " + budget[2] if budget[2] else "") + "。")
                sources.append(dict(document_id=material["document_id"], revision_id=material["revision_id"],
                                    parse_id=material["parse_id"], ordinal=unit["ordinal"]))
            args["pages"] = [dict(title="链路预算要点", category="工程/射频", blocks=[dict(
                content="\n".join(facts), sources=sources)], related_page_ids=[], depends_on=[])]
            return call("knowledge_ingest", args)
        stats["refs_count"] = len(receipt.get("result_refs", []))
        success = (receipt.get("status") == "waiting_input" if purpose == "waiting" else
                   receipt.get("status") == "succeeded" and receipt.get("outcome") ==
                   ("published" if purpose == "long_term" else "transient"))
        if purpose == "long_term":
            success = success and stats["pages_submitted"] and stats["refs_count"] > 0 and all(
                ref.get("searchable") and ref.get("index_mode") == "keyword" for ref in receipt["result_refs"])
        return finish(("INGEST_OK " if success else "INGEST_NOT_SUCCEEDED ") + json.dumps(receipt, ensure_ascii=False), success)
    if "P01_SEARCH" in text:
        query = text.split("P01_SEARCH", 1)[1].strip() or "budget"
        replies = [v for n, _, v in returned if n == "memory_search"]
        return finish("SEARCH_RESULT " + json.dumps(replies[-1], ensure_ascii=False)) if replies else call("memory_search", {"query": query, "max_results": 10})
    if "P01_TOOL" in text:
        replies = [v for n, _, v in returned if n == "ls"]
        if replies:
            success = "P01_READONLY_SENTINEL.txt" in str(replies[-1])
            return finish("P01_TOOL_OK" if success else "P01_TOOL_UNVERIFIED " + str(replies[-1]), success)
        return call("ls", {"path": "."})
    return finish("P01_CHAT_OK")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_json({"object": "list", "data": [
            {"id": name, "object": "model", "owned_by": "p01-local"}
            for name in ("p01-chat", "p01-image")]})

    def send_json(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.headers.get("Authorization") not in (
            "Bearer p01-chat-dummy", "Bearer p01-image-dummy"):
            self.send_json({"error": "only dummy credentials accepted"}, 403)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        delta, stats = choose_response(body)
        record = {"time": time.time(), "path": self.path,
                  "protocol": "openai-chat-completions", "model": body.get("model"),
                  "provider": "chat-local" if self.headers["Authorization"].endswith("chat-dummy") else "image-local",
                  "stream": body.get("stream", False), **stats}
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        finish = "tool_calls" if "tool_calls" in delta else "stop"
        base = {"id": "chatcmpl-p01", "created": int(time.time()), "model": body.get("model")}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for d, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish)):
                chunk = {**base, "object": "chat.completion.chunk", "choices": [
                    {"index": 0, "delta": d, "finish_reason": reason}]}
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_json({**base, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", **delta}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def verify_installed(base):
    """Explicit localhost-only installation probe; no real account required."""
    import hashlib
    import requests
    from urllib.parse import urlparse

    url = urlparse(base)
    if url.scheme != "http" or url.hostname != "127.0.0.1":
        raise ValueError("安装探针仅允许 http://127.0.0.1")
    session = requests.Session()
    session.trust_env = False

    def get_task(task_id):
        return session.get(base + "/api/personal/tasks/" + task_id, timeout=10).json()["task"]

    def submit(name, content, text):
        receipt = session.post(base + "/api/personal/tasks", files={"file": (name, content)},
                               data={"request_id": "installed-p01-" + name, "text": text}, timeout=30).json()
        task = receipt["task"]
        for _ in range(60):
            if task["status"] in {"succeeded", "failed", "waiting_input", "cancelled"}:
                break
            time.sleep(0.5)
            task = get_task(task["task_id"])
        return get_task(task["task_id"])

    rows, references = [], []
    for name in ("source.docx", "source.pdf", "source.xlsx"):
        content = (EVIDENCE.parent / "selftest-source-2/samples" / name).read_bytes()
        task = submit(name, content, "请整理这份资料")
        rows.append({"filename": name, "task": task})
        assert task["status"] == "succeeded" and task["response"].startswith("INGEST_OK")
        assert task["attachments"][0]["sha256"] == hashlib.sha256(content).hexdigest()
        for ref in task["result_refs"]:
            assert ref["current_searchable"] and ref["index_mode"] == "keyword"
            endpoint = base + "/api/personal/tasks/" + task["task_id"] + "/reference/" + ref["publish_id"]
            first = session.get(endpoint, timeout=10).json()["reference"]
            units = [session.get(endpoint, params={"ordinal": unit["ordinal"]}, timeout=10).json()["reference"]
                     for unit in first["units"]]
            references.append({"filename": name, "task_id": task["task_id"], "reference": first, "units": units})
    transient = submit("p01-transient.txt", b"P01_TRANSIENT_SECRET_4719 budget private", "P01_TRANSIENT")
    assert transient["status"] == "succeeded" and transient["outcome"] == "transient"
    assert all(not r["current_searchable"] for r in transient["result_refs"])
    searches = []
    for query in ("budget", "P01_TRANSIENT_SECRET_4719"):
        sid = "installed-search-" + query + "-" + str(time.time_ns())
        sent = session.post(base + "/message", json={"session_id": sid, "message": "P01_SEARCH " + query,
                                                   "stream": False}, timeout=20).json()
        for _ in range(60):
            time.sleep(0.5)
            response = session.post(base + "/poll", json={"session_id": sid}, timeout=10).json()
            if response.get("has_content") and response.get("request_id") == sent["request_id"]:
                break
        assert response.get("has_content") and response["content"].startswith("SEARCH_RESULT ")
        searches.append({"query": query, "result": response})
    assert all(r["task"]["result_refs"][0]["publish_id"] in searches[0]["result"]["content"] for r in rows)
    assert "No memories found" in searches[1]["result"]["content"]
    for name, value in (("installed-tasks", rows), ("installed-references", references),
                        ("installed-transient", transient), ("installed-search", searches)):
        (EVIDENCE / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    assertions = {"three_formats_succeeded": True, "original_hashes": True, "keyword_indexes": True,
                  "references_read_back": True, "search_contains_three_source_ids": True,
                  "transient_archived_not_recalled": True}
    (EVIDENCE / "installed-assertions.json").write_text(json.dumps(assertions, indent=2), encoding="utf-8")
    print(json.dumps(assertions, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["serve", "check", "installed"])
    p.add_argument("--port", type=int, default=19877)
    p.add_argument("--base", default="http://127.0.0.1:9876")
    args = p.parse_args()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if args.action == "serve":
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    elif args.action == "installed":
        verify_installed(args.base.rstrip("/"))
    else:
        rows = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()]
        assertions = {"chat_model": any(r["model"] == "p01-chat" for r in rows),
                      "stream": any(r["stream"] for r in rows),
                      "readonly_tool_called": any(r["tool_called"] == "ls" for r in rows),
                      "readonly_tool_result": any(r["tool_success"] for r in rows),
                      "skill_body_read": any(r.get("skill_read") for r in rows),
                      "attachments_read": any(r.get("attachment_reads", 0) for r in rows),
                      "organized_pages_executed": any(r.get("pages_submitted") and r["tool_success"] for r in rows),
                      "no_embedding_on_stub": all("embedding" not in r["path"] for r in rows)}
        (EVIDENCE / "assertions.json").write_text(json.dumps(assertions, indent=2), encoding="utf-8")
        print(json.dumps(assertions, indent=2))
        raise SystemExit(0 if all(assertions.values()) else 1)


if __name__ == "__main__":
    main()
