"""O-01/P-01: isolated real Agent/Responses round trip, never publishes knowledge.

The only experimental seam adds an inline PDF to its function_call_output at
the HTTP boundary. No application configuration or production delivery code is
changed. Reports contain hashes and counts, never credentials or file data.
"""
import argparse
import base64
import copy
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import MethodType


def run(config_path, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["COW_DATA_DIR"] = str(output / "runtime")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader
    from config import Config, config_scope
    from common.log import logger
    from common.personal_tasks import OWNER, TaskStore, task_scope
    from common.runtime_identity import identity_scope
    from models.config_revision import capture_config_revision, use_config_revision
    from bridge.agent_bridge import AgentLLMModel
    from bridge.bridge import Bridge
    from agent.protocol import Agent
    from agent.tools.base_tool import BaseTool, ToolResult
    from agent.tools.tool_manager import ToolManager

    cfg = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    # No user MCP sessions, fallbacks or background state in this bounded probe.
    cfg["chat_fallback"] = {"chain": []}
    cfg["debug"] = False
    logger.setLevel("WARNING")
    code = str(secrets.randbelow(900000) + 100000)
    image = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 64)
    draw.text((45, 35), "CHECK: " + code, font=font, fill="black")
    for x in (150, 450, 750):
        draw.polygon([(x, 200), (x - 65, 330), (x + 65, 330)], fill="blue")
    for x in (300, 600):
        draw.ellipse((x - 50, 430, x + 50, 530), fill="red")
    pdf = output / "synthetic-image-only.pdf"
    image.save(pdf, "PDF", resolution=120)
    raw = pdf.read_bytes()
    assert not PdfReader(pdf).pages[0].extract_text().strip()
    reading_id = "probe-" + secrets.token_hex(12)
    report = dict(schema_version=1, time=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                  model=cfg["model"], config_revision=None, reading_id=reading_id,
                  sha256=hashlib.sha256(raw).hexdigest(), file_bytes=len(raw),
                  text_layer_characters=0, requests=[], observations=[],
                  production_delivery_enabled=False, published=False)
    offered = []
    nonce = secrets.token_hex(10)

    class ReadPDF(BaseTool):
        name = "read_pdf_probe"
        description = "读取本任务唯一的无敏感PDF探针，以实际媒体返回。"
        params = {"type": "object", "properties": {}}

        def execute(self, args):
            offered.append(self.tool_call_id)
            return ToolResult.success(dict(reading_id=reading_id, segment_id="page-1",
                physical_page=1, media="随本工具结果提供", status="prepared"))

    class ObservePDF(BaseTool):
        name = "record_pdf_observation"
        description = "提交从PDF图像中读到的六位数字及蓝色三角形、红色圆形的数量。"
        params = {"type": "object", "properties": {
            "reading_id": {"type": "string"}, "code": {"type": "string"},
            "blue_triangles": {"type": "integer"}, "red_circles": {"type": "integer"}},
            "required": ["reading_id", "code", "blue_triangles", "red_circles"]}

        def execute(self, args):
            ok = args == dict(reading_id=reading_id, code=code, blue_triangles=3, red_circles=2)
            report["observations"].append(dict(correct=ok, reading_id=args.get("reading_id")))
            return ToolResult.success(dict(correct=ok, receipt=nonce))

    with config_scope(Config(cfg)):
        revision = capture_config_revision()
        report["config_revision"] = revision
        store = TaskStore(output / "tasks")
        task = store.submit(OWNER, "pdf-native-agent-probe", {"text": "无敏感媒体探针"},
                            {"channel": "web"}, revision)
        store.attach(OWNER, task["task_id"], pdf)
        task = store.claim(OWNER, task["task_id"])
        report.update(task_id=task["task_id"], attempt=task["attempt"])
        with use_config_revision(revision), identity_scope(user_id=OWNER), task_scope(task["task_id"]):
            model = AgentLLMModel(Bridge(), bot_type=cfg["bot_type"])
            from agent.knowledge.reading_capabilities import capability_identity
            report["provider_identity"] = capability_identity(model, revision)
            bot = model.bot
            original = bot._handle_responses_stream
            delivered = set()

            def send(_bot, payload, api_key, api_base, timeout, model_name):
                payload = copy.deepcopy(payload)
                media_calls = []
                for item in payload.get("input", []):
                    call_id = item.get("call_id")
                    if item.get("type") == "function_call_output" and call_id in offered and call_id not in delivered:
                        item["output"] = [{"type": "input_text", "text": item["output"]},
                            {"type": "input_file", "filename": "probe.pdf",
                             "file_data": "data:application/pdf;base64," + base64.b64encode(raw).decode("ascii")}]
                        delivered.add(call_id)
                        media_calls.append(call_id)
                record = dict(protocol="responses", streaming=True, media_call_ids=media_calls,
                              input_types=[i.get("type", i.get("role")) for i in payload["input"]],
                              tool_definition_count=len(payload.get("tools") or []),
                              max_output_tokens=payload.get("max_output_tokens"),
                              reasoning=payload.get("reasoning"),
                              request_bytes=len(json.dumps(payload).encode("utf-8")), usage=None)
                report["requests"].append(record)
                start = time.perf_counter()
                try:
                    for chunk in original(payload, api_key, api_base, (90, 240), model_name):
                        if chunk.get("usage"):
                            record["usage"] = chunk["usage"]
                        if chunk.get("error"):
                            record["error"] = True
                        yield chunk
                finally:
                    record["elapsed_s"] = round(time.perf_counter() - start, 3)

            bot._handle_responses_stream = MethodType(send, bot)
            sync = ToolManager.sync_mcp_into_agent
            ToolManager.sync_mcp_into_agent = lambda *a: None
            start = time.perf_counter()
            try:
                agent = Agent("按顺序调用read_pdf_probe，直接阅读工具提供的PDF图像，再调用"
                    "record_pdf_observation填写所见数字与形状数量。最后只输出该工具返回的receipt。"
                    "无法看见图像时如实说明。", model=model, tools=[ReadPDF(), ObservePDF()],
                    workspace_dir=str(output), max_steps=4, enable_skills=False,
                    skip_context_files=True, output_mode="logger")
                result = agent.run_stream("执行本任务的图文读取和工具往返验证。")
                report["receipt_round_trip"] = nonce in result
                report["passed"] = (len(offered) == 1 and len(report["observations"]) == 1
                    and report["observations"][0]["correct"] and report["receipt_round_trip"]
                    and sum(len(r["media_call_ids"]) for r in report["requests"]) == 1)
            except Exception as exc:
                # Error strings from endpoints can contain request bodies.
                report.update(passed=False, exception_type=type(exc).__name__)
            finally:
                ToolManager.sync_mcp_into_agent = sync
                bot._handle_responses_stream = original
                report["elapsed_s"] = round(time.perf_counter() - start, 3)
                (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.config, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("passed") else 1)
