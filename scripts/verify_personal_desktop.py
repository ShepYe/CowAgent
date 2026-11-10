"""P-01 本机替身；真实桌面操作证据存入 local/p01-evidence。

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


def choose_response(body):
    """Only this user turn selects intent; historical tool replies cannot finish it."""
    messages = body.get("messages", [])
    user_index = next((i for i in range(len(messages) - 1, -1, -1)
                       if messages[i].get("role") == "user"
                       and content_text(messages[i].get("content"))), -1)
    text = content_text(messages[user_index].get("content")) if user_index >= 0 else ""
    tools = {t.get("function", {}).get("name") for t in body.get("tools", [])}
    returned = [m for m in messages[user_index + 1:] if m.get("role") == "tool"]
    task = re.search(r"\[系统接收记录\]\s*task_id=([A-Za-z0-9_-]+)", text)
    name, args = None, {}
    if task:
        # The appended receipt/instructions and system prompt never decide purpose.
        purpose = "transient" if "P01_TRANSIENT" in text[:task.start()] else "long_term"
        name, args = "knowledge_ingest", {"task_id": task[1], "purpose": purpose,
                    "reason": "本机替身：按用户测试标记选择用途"}
    elif "P01_SEARCH" in text:
        query = text.split("P01_SEARCH", 1)[1].strip() or "budget"
        name, args = "memory_search", {"query": query, "max_results": 10}
    elif "P01_TOOL" in text:
        name, args = "ls", {"path": "."}
    result = None
    for reply in reversed(returned):
        # Resolve the returned ID against the assistant's actual function call.
        reply_id = reply.get("tool_call_id")
        calls = [c for m in messages[user_index + 1:] for c in m.get("tool_calls", [])
                 if c.get("id") == reply_id and c.get("function", {}).get("name") == name]
        if calls:
            result = content_text(reply.get("content"))
            break
    success, refs_count = False, 0
    if result is not None:
        if name == "knowledge_ingest":
            try:
                receipt = json.loads(result)
                receipt = receipt.get("result", receipt)
                if isinstance(receipt, str):
                    receipt = json.loads(receipt)
                success = receipt.get("status") == "succeeded" and receipt.get("task_id") == task[1]
                refs = receipt.get("result_refs", [])
                refs_count = len(refs)
                answer = ("INGEST_OK " if success else "INGEST_NOT_SUCCEEDED ") + json.dumps(
                    receipt, ensure_ascii=False)
            except (ValueError, TypeError, AttributeError):
                answer = "INGEST_UNVERIFIED " + result
        elif name == "memory_search":
            # Verbatim real result, including empty/error results; invent no refs.
            answer = "SEARCH_RESULT " + result
            success = not result.startswith("Error")
        else:
            success = "P01_READONLY_SENTINEL.txt" in result
            answer = "P01_TOOL_OK" if success else "P01_TOOL_UNVERIFIED " + result
        delta = {"content": answer}
    elif name and name in tools:
        delta = {"tool_calls": [{"index": 0, "id": "call_p01_" + name, "type": "function",
                                 "function": {"name": name, "arguments": json.dumps(args)}}]}
    else:
        delta = {"content": "P01_TOOL_UNAVAILABLE " + name if name else "P01_CHAT_OK"}
    return delta, {"tool_offered": "ls" in tools,
                   "ingest_offered": "knowledge_ingest" in tools,
                   "search_offered": "memory_search" in tools,
                   "tool_called": name if "tool_calls" in delta else None,
                   "purpose": args.get("purpose"), "tool_returned": len(returned),
                   "tool_success": success, "refs_count": refs_count}


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
                      "no_embedding_on_stub": all("embedding" not in r["path"] for r in rows)}
        (EVIDENCE / "assertions.json").write_text(json.dumps(assertions, indent=2), encoding="utf-8")
        print(json.dumps(assertions, indent=2))
        raise SystemExit(0 if all(assertions.values()) else 1)


if __name__ == "__main__":
    main()
