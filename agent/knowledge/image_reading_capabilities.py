"""图片 MIME 独立能力探针；两轮真实 tool call，只发送无敏感随机数字图。

ensure_image_capability(workspace, model, revision, mime_type) -> bool
仅 supported 凭据可复用；失败保存 unknown，本次不重试，后续显式调用可以重新探测。
身份绑定 Provider／端点／模型／协议／配置修订／实际 MIME／inline，不借 PDF 凭据。
transport context.mime_type 目前固定 PDF，故明确忽略此字段；实际 MIME 来自本地
fixture 和 ToolMediaContent，参与独立缓存身份。只使用 read_probe，不改 PDF 模块。
"""
from io import BytesIO
import hashlib
import json
import secrets

from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.reading_capabilities import (
    _key, _probe_output_budget, _request_probe_call, capability_identity,
)


MIME_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}
PROFILE = "image-900x600-arial64-random-code-v1"
PROBE_VERSION = 1
_SYSTEM = "先调用read_probe获取无敏感测试图像，然后只输出图片中的六位数字；看不清输出unknown。"
_TOOLS = [dict(name="read_probe", description="获取无敏感能力测试图像",
               input_schema={"type": "object", "properties": {}, "additionalProperties": False})]
_USER = dict(role="user", content="请调用read_probe并读取测试图像。")


def image_capability_identity(model, revision, mime_type):
    """仅派生路由身份，不读取 PDF 支持记录；非法 MIME 不触发模型或数据库。"""
    if mime_type not in MIME_FORMATS:
        raise ValueError("unsupported_image_mime")
    identity = capability_identity(model, revision)
    return dict(identity, mime_type=mime_type) if identity is not None else None


def _probe_image(code, mime_type):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((45, 35), "CHECK: " + code, font=ImageFont.truetype("arial.ttf", 64), fill="black")
    for x in (150, 450, 750):
        draw.polygon([(x, 200), (x - 65, 330), (x + 65, 330)], fill="blue")
    for x in (300, 600):
        draw.ellipse((x - 50, 430, x + 50, 530), fill="red")
    stream = BytesIO()
    image.save(stream, MIME_FORMATS[mime_type])
    payload = stream.getvalue()
    if not payload or len(payload) > 128 * 1024:
        raise ValueError("image_probe_fixture_budget_exceeded")
    return payload


def ensure_image_capability(workspace, model, revision, mime_type):
    """一次调用至多两次请求；不调用 PDF ensure、不修改 catalog、不持久化图像或答案。"""
    if mime_type not in MIME_FORMATS:
        raise ValueError("unsupported_image_mime")
    from models.config_revision import use_config_revision
    with use_config_revision(revision):
        return _ensure_image_capability(workspace, model, revision, mime_type)


def _ensure_image_capability(workspace, model, revision, mime_type):
    from agent.protocol import LLMRequest
    from agent.protocol.tool_media import ToolMediaContent, ToolMediaError, ToolMediaReference, stream_with_tool_media
    from models.config_revision import require_config_revision

    identity = image_capability_identity(model, revision, mime_type)
    if identity is None:
        return False
    key = _key(identity)
    service = SourceService(workspace)
    with service.transaction() as db:
        db.execute("CREATE TABLE IF NOT EXISTS reading_capabilities(id TEXT PRIMARY KEY,record TEXT)")
        row = db.execute("SELECT record FROM reading_capabilities WHERE id=?", (key,)).fetchone()
    if row:
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            record = None
        if (isinstance(record, dict) and type(record.get("schema_version")) is int
                and record.get("schema_version") == 1 and type(record.get("probe_version")) is int
                and record.get("probe_version") == PROBE_VERSION and record.get("identity") == identity
                and record.get("probe_kind") == "image" and record.get("fixture_profile") == PROFILE
                and record.get("verified_image") is True and record.get("state") == "supported"):
            return True
    code = str(secrets.randbelow(900000) + 100000)
    states, response_text, error, loaded = [], "", False, False
    fixture_hash = None

    def supports(context):
        try:
            # 当前通道 mime_type 字段错误地固定为 application/pdf；不可用它做图片身份。
            return (image_capability_identity(model, revision, mime_type) == identity
                and context.get("provider", identity["provider"]) == identity["provider"]
                and context.get("transport", "inline") == "inline"
                and context.get("protocol") == "responses" and context.get("model") == identity["model"]
                and context.get("config_revision") == revision
                and hashlib.sha256(str(context.get("api_base") or "").rstrip("/").encode()).hexdigest()
                == identity["endpoint_hash"])
        except Exception:
            return False

    def load(context):
        nonlocal loaded
        require_config_revision(revision)
        if not supports(context):
            raise ToolMediaError("attachment_unsupported")
        loaded = True
        return ToolMediaContent(mime_type, raw)

    ref = ToolMediaReference(load, supports, states.append)
    stream = None
    try:
        raw = _probe_image(code, mime_type)
        fixture_hash = hashlib.sha256(raw).hexdigest()
        # 复用真实首轮调用收集器；其 PDF 提示术语不改变 read_probe 的身份与顺序。
        assistant = _request_probe_call(model)
        require_config_revision(revision)
        if image_capability_identity(model, revision, mime_type) != identity:
            raise ToolMediaError("capability_probe_config_changed")
        call = next(item for item in assistant["content"] if item.get("type") == "tool_use")
        request = LLMRequest(system=_SYSTEM, messages=[dict(_USER), assistant,
            dict(role="user", content=[dict(type="tool_result", tool_use_id=call["id"], content="本次无敏感测试图像")])],
            tools=_TOOLS, max_tokens=_probe_output_budget(model), stream=True)
        stream = iter(stream_with_tool_media(model, request, {call["id"]: (ref,)}))
        for chunk in stream:
            if chunk.get("error"):
                error = True
                break
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("tool_calls") or choice.get("finish_reason") in ("length", "tool_calls", "content_filter"):
                    error = True
                response_text += delta.get("content") or ""
        require_config_revision(revision)
        error = error or image_capability_identity(model, revision, mime_type) != identity
    except Exception:
        error = True
    finally:
        if stream is not None and hasattr(stream, "close"):
            try:
                stream.close()
            except Exception:
                error = True
    state = "supported" if (not error and loaded and states == ["sent", "completed"]
                             and response_text.strip() == code) else "unknown"
    record = dict(schema_version=1, probe_kind="image", probe_version=PROBE_VERSION, identity=identity,
                  fixture_profile=PROFILE, fixture_hash=fixture_hash,
                  state=state, verified_image=state == "supported")
    with service.transaction() as db:
        db.execute("INSERT OR REPLACE INTO reading_capabilities VALUES(?,?)", (key, json.dumps(record)))
    return state == "supported"
