"""运行期工具媒体：历史只保存文本，字节仅在已验证的请求边界展开。

reading service 必须在 P03/恢复验收后才创建引用。loader 每次调用均须
核验 owner/session/task/attempt/config/source/hash 及预算；supports 核验
实际 Provider、模型、协议、媒体类型与配置修订。此模块不授予文件权限。
"""
import base64
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable


class ToolMediaError(ValueError):
    """只携带安全错误码，禁止传播 loader 或服务端可能包含材料的异常。"""


def media_request_error(error=None, status_code=None):
    """保留状态与可行动类别，不传播可能回显请求体的服务端字符串。"""
    status = status_code or getattr(error, "status_code", None) or 500
    detail = str(getattr(error, "message", error) or "").lower()
    if any(word in detail for word in ("context_length", "context window", "maximum context", "too many tokens")) or status == 413:
        code = "reading_budget_exceeded"
    elif status == 429:
        code = "rate_limited"
    elif status in (408, 504) or "timeout" in type(error).__name__.lower():
        code = "media_request_timeout"
    elif status == 400:
        code = "media_request_rejected"
    else:
        code = "media_request_failed"
    result = {"error": {"code": code, "message": code}, "message": code, "status_code": status}
    if code == 'reading_budget_exceeded':
        result['error']['budget_kind'] = 'input_bytes' if status == 413 else 'input_context'
    # 仅保留已知、无内容的网关错误码，便于区分上游断流与本地传输失败。
    upstream = error.get('code') if isinstance(error, dict) else getattr(error, 'code', None)
    if upstream in ('upstream_stream_break', 'server_error', 'rate_limit_exceeded',
                    'context_length_exceeded', 'request_too_large'):
        result['error']['upstream_code'] = upstream
    return result


@dataclass(frozen=True, repr=False)
class ToolMediaContent:
    mime_type: str
    data: bytes


@dataclass(frozen=True, repr=False)
class ToolMediaReference:
    loader: Callable
    supports: Callable = field(default=lambda context: False)
    on_delivery: Callable = field(default=lambda state: None)
    # 仅真实响应输出；保存成功后才能声明 completed。旧纯传输调用方可省略。
    on_response: Callable = None
    on_error: Callable = None


_pending = ContextVar("tool_media_pending", default=None)
_provider = ContextVar("tool_media_provider", default=None)


def current_tool_media():
    return _pending.get() or {}


def current_media_provider():
    return _provider.get()


def stream_with_tool_media(model, request, pending):
    """仅在推进模型生成器时绑定引用；取消时确定性关闭底层请求。"""
    if pending:
        from models.openai.responses_adapter import use_responses_api
        from models.openai_compatible_bot import OpenAICompatibleBot
        bot = getattr(model, "bot", None)
        if not isinstance(bot, OpenAICompatibleBot):
            raise ToolMediaError("attachment_unsupported")
        config = bot.get_api_config() if hasattr(bot, "get_api_config") else {}
        if not use_responses_api(getattr(model, "model", ""), config.get("api_type")):
            raise ToolMediaError("attachment_unsupported")
    stream = None
    resolve = getattr(model, "_resolve_bot_type", None)
    provider = resolve(model.model) if pending and callable(resolve) else getattr(model, "bot_type", None)
    try:
        while True:
            token = _pending.set(pending)
            provider_token = _provider.set(provider)
            try:
                if stream is None:
                    stream = iter(model.call_stream(request))
                chunk = next(stream)
            except StopIteration:
                return
            finally:
                _provider.reset(provider_token)
                _pending.reset(token)
            yield chunk
    finally:
        if stream is not None and hasattr(stream, "close"):
            stream.close()


class MediaDelivery:
    """一次请求的生命周期。sent 是即将交给 HTTP，结果不确定即 unknown。"""
    def __init__(self):
        self.refs = []
        self.started = False
        self.finished = False
        self.error_reported = False

    def notify(self, state):
        failed = False
        for ref in self.refs:
            try:
                ref.on_delivery(state)
            except Exception:
                failed = True
        if failed:
            raise ToolMediaError("media_receipt_failed") from None

    def start(self):
        self.started = True
        self.notify("sent")

    def save_response(self, response):
        import copy
        metadata = {key: response[key] for key in ("id", "output", "usage", "status") if key in response}
        try:
            for ref in self.refs:
                if ref.on_response is not None:
                    ref.on_response(copy.deepcopy(metadata))
        except Exception:
            raise ToolMediaError("media_response_checkpoint_failed") from None

    def finish(self, completed):
        if self.started and not self.finished:
            self.finished = True
            self.notify("completed" if completed else "unknown")

    def report_error(self, classified):
        """先落未知状态，再把安全分类交给受信阅读器；不传服务器原文。"""
        self.finish(False)
        if self.error_reported:
            return
        self.error_reported = True
        failed = False
        for ref in self.refs:
            if ref.on_error is not None:
                try:
                    ref.on_error(dict(classified.get('error') or {}))
                except Exception:
                    failed = True
        if failed:
            raise ToolMediaError('media_receipt_failed') from None


def expand_tool_media(payload, pending, context):
    """原地修改独立请求体；消费后不因后续轮次/自动重试再次发送。"""
    delivery = MediaDelivery()
    selected = []
    try:
        for item in payload.get("input", []):
            call_id = item.get("call_id")
            if item.get("type") != "function_call_output" or call_id not in pending:
                continue
            refs = pending[call_id]
            parts = [{"type": "input_text", "text": item["output"]}]
            for ref in refs:
                if context["protocol"] != "responses" or ref.supports(context) is not True:
                    raise ToolMediaError("attachment_unsupported")
                media = ref.loader(context)
                if not isinstance(media, ToolMediaContent) or not isinstance(media.data, bytes) or not media.data:
                    raise ToolMediaError("invalid_tool_media")
                url = "data:" + media.mime_type + ";base64," + base64.b64encode(media.data).decode("ascii")
                if media.mime_type == "application/pdf":
                    if not media.data.startswith(b"%PDF-"):
                        raise ToolMediaError("invalid_tool_media")
                    parts.append({"type": "input_file", "filename": "document.pdf", "file_data": url})
                elif media.mime_type in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    parts.append({"type": "input_image", "image_url": url})
                else:
                    raise ToolMediaError("attachment_unsupported")
                delivery.refs.append(ref)
            item["output"] = parts
            selected.append(call_id)
    except ToolMediaError:
        raise
    except Exception:
        raise ToolMediaError("media_validation_failed") from None
    if pending and not selected:
        raise ToolMediaError("media_context_lost")
    for call_id in selected:
        del pending[call_id]
    return delivery
