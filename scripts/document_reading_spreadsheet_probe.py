"""一次真实 XLSX 主 Agent 看图探针：只用 Read，不登记长期图像观察。

配置只在内存隔离；输出须为尚不存在的 tmp 目录。默认五次，修复重测可限七次请求，
任何请求失败后停止，禁止重新付费尝试；不改写模型目录或图片能力声明。
"""
import argparse
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import time


class ProbeStop(BaseException):
    """绕过 Agent 的普通异常重试，只终止本次探针。"""


def make_fixture(workspace):
    from PIL import Image, ImageDraw, ImageFont
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
    code = str(secrets.randbelow(900000) + 100000)
    picture = workspace / "embedded.png"
    image = Image.new("RGB", (1000, 480), "white")
    draw = ImageDraw.Draw(image)
    font = "C:/Windows/Fonts/msyh.ttc"
    draw.rectangle((20, 20, 980, 460), outline="#156b72", width=6)
    draw.text((70, 65), "离线嵌图核验：数字仅在图片中", fill="#153e51", font=ImageFont.truetype(font, 40))
    draw.text((180, 200), code, fill="black", font=ImageFont.truetype(font, 112))
    image.save(picture)
    book = Workbook()
    sheet = book.active
    sheet.title = "链路校验"
    sheet["A1"] = "请读取嵌入图片；单元格不保存图片中的数字。"
    sheet["A2"] = "图片锚点以 OOXML 结构回执为准。"
    embedded = XLImage(picture)
    embedded.anchor = TwoCellAnchor(_from=AnchorMarker(col=3, row=4, colOff=1234, rowOff=5678),
                                   to=AnchorMarker(col=9, row=15, colOff=9012, rowOff=3456))
    sheet.add_image(embedded)
    source = workspace / "probe.xlsx"
    book.save(source)
    book.close()
    # PNG 独有数字不留在普通文件或文件名；图片字节仅保留在 XLSX 包内。
    raw = picture.read_bytes()
    picture.unlink()
    return source, dict(code=code, worksheet=sheet.title, anchor_from="D5", anchor_to="J16",
                        image_sha256=sha256(raw).hexdigest(), image_bytes=len(raw),
                        source_sha256=sha256(source.read_bytes()).hexdigest())


def run(config_path, output, *, reuse_report=None, max_requests=5):
    repo = Path(__file__).resolve().parents[1]
    output = output.resolve()
    if not output.is_relative_to(repo / "tmp") or output.exists():
        raise ValueError("output 必须为项目 tmp 内尚不存在的唯一目录")
    output.mkdir(parents=True)
    workspace = output / "runtime"
    workspace.mkdir()
    previous_data = os.environ.get("COW_DATA_DIR")
    os.environ["COW_DATA_DIR"] = str(workspace)
    sys.path.insert(0, str(repo))
    from config import Config, config_scope
    from common.log import logger
    from common.runtime_identity import identity_scope
    from models.config_revision import capture_config_revision, use_config_revision
    from models.openai.openai_http_client import OpenAIHTTPClient
    from bridge.agent_bridge import AgentLLMModel
    from bridge.bridge import Bridge
    from agent.protocol import Agent
    from agent.tools.read.read import Read
    from agent.tools.tool_manager import ToolManager
    from agent.knowledge.spreadsheet_materials import prepare_spreadsheet_materials

    if max_requests not in (5, 7):
        raise ValueError("max_requests 仅允许5或7")
    if reuse_report is None:
        source, expected = make_fixture(workspace)
    else:
        previous_report = json.loads(reuse_report.read_text(encoding="utf-8"))
        expected = previous_report["expected"]
        source = workspace / "probe.xlsx"
        shutil.copyfile(reuse_report.parent / "runtime/probe.xlsx", source)
        if sha256(source.read_bytes()).hexdigest() != expected["source_sha256"]:
            raise ValueError("复用样本 hash 不匹配")
    local = prepare_spreadsheet_materials(workspace, source.name)
    assert len(local.images) == 1 and local.images[0]["content_hash"] == expected["image_sha256"]
    config_before = sha256(config_path.read_bytes()).hexdigest()
    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    cfg.update(agent_workspace=str(workspace), workspace_root=str(workspace), debug=False,
               chat_fallback={"chain": []}, responses_prompt_cache_enabled=False,
               embedding_enabled=False, knowledge_semantic_enabled=False)
    report = dict(run_count=1, max_requests=max_requests, requests=[], tools=[], expected=expected,
                  image_capability_overridden=False, responses_prompt_cache_enabled=False,
                  long_term_observations_published=False, passed=False)
    if reuse_report is not None:
        report["previous_report"] = str(reuse_report.resolve())
        report["retest_reason"] = "新增独立图片能力两轮探针，Read已接入未知能力探测；不修改catalog"
    original = OpenAIHTTPClient.responses
    original_sync = ToolManager.sync_mcp_into_agent
    prior_level = logger.level
    failed = False

    def traced(client, **kwargs):
        nonlocal failed
        if failed or len(report["requests"]) >= report["max_requests"]:
            raise ProbeStop("request_failed_no_retry" if failed else "request_budget_exhausted")
        row = dict(media=[], usage=None, elapsed_s=None,
                   request_bytes=len(json.dumps({k: v for k, v in kwargs.items()
                       if k not in ("api_key", "api_base", "extra_headers")}, default=str).encode("utf-8")),
                   prompt_cache_key_present="prompt_cache_key" in kwargs)
        for item in kwargs.get("input", []):
            for key in ("content", "output"):
                for part in item.get(key) if isinstance(item.get(key), list) else []:
                    if part.get("type") not in ("input_image", "input_file"):
                        continue
                    value = part.get("image_url") or part.get("file_data")
                    if not isinstance(value, str) or not value.startswith("data:"):
                        raise ProbeStop("unexpected_non_inline_media")
                    raw = base64.b64decode(value.split(",", 1)[1], validate=True)
                    row["media"].append(dict(type=part["type"], sha256=sha256(raw).hexdigest(), bytes=len(raw)))
        report["requests"].append(row)
        start = time.monotonic()
        try:
            for event in original(client, **kwargs):
                response = event.get("response") or {}
                if response.get("usage"):
                    row["usage"] = response["usage"]
                kind = event.get("type")
                if kind in ("response.completed", "response.failed", "response.incomplete"):
                    row["terminal"] = kind
                if kind in ("response.failed", "response.incomplete", "error") or event.get("error"):
                    failed = True
                    error = event.get("error") or response.get("error") or {}
                    row["error_code"] = error.get("code") if isinstance(error, dict) else "request_failed"
                yield event
        except Exception as exc:
            failed = True
            row["exception_type"] = type(exc).__name__
            raise ProbeStop("request_failed_no_retry") from None
        finally:
            row["elapsed_s"] = round(time.monotonic() - start, 3)

    def event(envelope):
        kind, data = envelope.get("type"), envelope.get("data") or {}
        if kind == "tool_execution_start":
            report["tools"].append(dict(call_id=data.get("tool_call_id"), name=data.get("tool_name"),
                                         arguments=data.get("arguments")))
        elif kind == "tool_execution_end":
            row = next((r for r in reversed(report["tools"]) if r.get("call_id") == data.get("tool_call_id")), None)
            if row is None:
                row = dict(call_id=data.get("tool_call_id"), name=data.get("tool_name"))
                report["tools"].append(row)
            row.update(status=data.get("status"), elapsed_s=data.get("execution_time"))
            result = data.get("result")
            if isinstance(result, dict):
                # 只记录原件对象绑定、媒体状态和 gap；不保存私有配置或整个请求。
                sheet = result.get("spreadsheet") or {}
                row.update(original_hash=sheet.get("original_hash"), images=sheet.get("images"),
                           media_map=sheet.get("media_map"), media_attached=sheet.get("media_attached"),
                           coverage_gaps=sheet.get("coverage_gaps"), result_code=result.get("code"))

    logger.setLevel("WARNING")
    OpenAIHTTPClient.responses = traced
    ToolManager.sync_mcp_into_agent = lambda *a: None
    start = time.monotonic()
    answer = ""
    try:
        with config_scope(Config(cfg)):
            revision = capture_config_revision()
            with use_config_revision(revision), identity_scope(user_id="local-owner", session_id=output.name):
                model = AgentLLMModel(Bridge(), bot_type=cfg["bot_type"])
                meta = model.catalog_model_meta() or {}
                report["catalog_capabilities"] = meta.get("capabilities") or []
                report["model"] = model.model
                read = Read(dict(cwd=str(workspace), workspace_root=str(workspace)))
                agent = Agent("", model=model, tools=[read], workspace_dir=str(workspace), max_steps=4,
                              enable_skills=False, skip_context_files=True, output_mode="logger")
                agent.extra_system_suffix = (
                    "本任务只使用read两次：先visual=false读取指定XLSX结构，再将回执中的准确object_key"
                    "填入objects、worksheet填准确名称，visual=true读取该图片。禁止猜测object_key。"
                    "图片未送达或能力未声明时说明gap并立即结束，不尝试其他路径、格式或重试。"
                    "若图片可见，回答图内六位数字、worksheet、anchor.from.cell和anchor.to.cell。"
                    "不创建知识，不发布图像观察，不读取其他文件。")
                answer = agent.run_stream(
                    "读取probe.xlsx：先read结构，再用object_key选中图像读取。"
                    "只回答图片中的六位核验数字及worksheet、单元格起止锚点；看不到则如实列gap。",
                    on_event=event)
    except ProbeStop as exc:
        report["stop_reason"] = str(exc)
    except Exception as exc:
        report["exception_type"] = type(exc).__name__
    finally:
        OpenAIHTTPClient.responses = original
        ToolManager.sync_mcp_into_agent = original_sync
        logger.setLevel(prior_level)
        if previous_data is None:
            os.environ.pop("COW_DATA_DIR", None)
        else:
            os.environ["COW_DATA_DIR"] = previous_data
        report["elapsed_s"] = round(time.monotonic() - start, 3)
        report["source_unchanged"] = sha256(source.read_bytes()).hexdigest() == expected["source_sha256"]
        report["config_file_unchanged"] = sha256(config_path.read_bytes()).hexdigest() == config_before
        media = [m for req in report["requests"] for m in req["media"]]
        images = [m for m in media if m["type"] == "input_image"]
        report["image_delivery_count"] = len(images)
        selected_images = [m for m in images if m["sha256"] == expected["image_sha256"]]
        report["selected_image_delivery_count"] = len(selected_images)
        report["capability_probe_image_count"] = len(images) - len(selected_images)
        report["request_count"] = len(report["requests"])
        report["actual_image_bytes_total"] = sum(m["bytes"] for m in images)
        report["actual_image_hashes"] = [m["sha256"] for m in images]
        report["actual_image_hash_verified"] = (len(selected_images) == 1
                                                 and selected_images[0]["bytes"] == expected["image_bytes"])
        successful = [t for t in report["tools"] if t.get("name") == "read" and t.get("status") == "success"]
        report["structure_then_selected_object"] = (len(successful) == 2
            and (successful[0].get("arguments") or {}).get("visual") is False
            and not successful[0].get("media_attached")
            and bool((successful[1].get("arguments") or {}).get("objects")))
        report["answer_checks"] = {key: expected[key] in answer for key in ("code", "worksheet", "anchor_from", "anchor_to")}
        report["usage_totals"] = {key: sum((r.get("usage") or {}).get(key, 0) for r in report["requests"])
                                   for key in ("input_tokens", "output_tokens", "total_tokens")}
        report["usage_missing_requests"] = sum(r.get("usage") is None for r in report["requests"])
        report["passed"] = (report["actual_image_hash_verified"] and report["structure_then_selected_object"]
            and all(report["answer_checks"].values()) and report["source_unchanged"] and report["config_file_unchanged"])
        report["outcome"] = ("image_read_passed" if report["passed"] else
            "image_capability_or_authorization_gap" if not images and any(
                g.get("code") == "image_media_not_authorized_or_supported"
                for t in report["tools"] for g in t.get("coverage_gaps") or []) else "probe_incomplete")
        (output / "answer.txt").write_text(answer, encoding="utf-8")
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-report", type=Path)
    parser.add_argument("--max-requests", type=int, default=5, choices=(5, 7))
    args = parser.parse_args()
    result = run(args.config, args.output, reuse_report=args.reuse_report, max_requests=args.max_requests)
    print(json.dumps({k: result[k] for k in ("passed", "elapsed_s", "image_delivery_count", "usage_totals")}, ensure_ascii=False))
    sys.exit(0 if result["passed"] else 1)
