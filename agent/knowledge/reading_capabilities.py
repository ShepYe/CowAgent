"""配置修订隔离的媒体能力；小型无敏感探针不读取用户资料。"""
import hashlib
import json
import secrets

from agent.knowledge.personal_assistant import SourceService
from common.log import logger

_PROBE_SYSTEM = '先调用read_probe获取无敏感PDF，再只输出PDF图像里的六位数字；看不清输出unknown。'
_PROBE_TOOLS = [dict(name='read_probe', description='获取无敏感能力测试PDF',
                     input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False})]
_PROBE_USER = dict(role='user', content='请调用read_probe并读取测试图像。')
_FIXTURE_PROFILE = 'p01-900x600-arial64-shapes-120dpi-v1'
_SAFE_ERROR_CODES = frozenset((
    'capability_probe_call_failed', 'capability_probe_call_missing',
    'capability_probe_call_invalid', 'capability_probe_config_changed',
    'attachment_unsupported', 'invalid_tool_media', 'media_validation_failed',
    'media_context_lost', 'media_receipt_failed', 'media_response_checkpoint_failed',
    'media_request_timeout', 'media_request_rejected', 'media_request_failed',
    'reading_budget_exceeded', 'rate_limited',
))


def _probe_output_budget(model):
    """与真实Agent的模型目录预算一致，不另设低额限制。

    只限制探针回答的内容和轮次；输出上限是能力配置，不是实际用量。
    """
    try:
        value = (model.catalog_model_meta() or {}).get('max_output_tokens')
        return int(value) if value else None
    except (AttributeError, ValueError, TypeError):
        return None


def _probe_pdf(code):
    """对齐 P01 的单页图像夹具；数字仅在像素中，输出不超过 128 KiB。

    保留三个蓝三角形、两个红圆作为版面内容，回答仍只核对随机数字。
    缺 Arial 时在首轮请求前失败，不悄悄替换字体或发起云端调用。
    """
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (900, 600), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('arial.ttf', 64)
    draw.text((45, 35), 'CHECK: ' + code, font=font, fill='black')
    for x in (150, 450, 750):
        draw.polygon([(x, 200), (x - 65, 330), (x + 65, 330)], fill='blue')
    for x in (300, 600):
        draw.ellipse((x - 50, 430, x + 50, 530), fill='red')
    stream = BytesIO()
    image.save(stream, 'PDF', resolution=120)
    raw = stream.getvalue()
    if len(raw) > 128 * 1024:
        raise ValueError('capability_probe_fixture_too_large')
    return raw


def _request_probe_call(model):
    """仅一轮请求，保留真实流式调用和文字；不生成或猜测 call_id。

    返回可接续的 assistant 消息。缺失／多调用、错误、截断或非法参数
    均拒绝，不通过重试或合成历史修补。测试可替换此 API。
    """
    from agent.protocol import LLMRequest
    from agent.protocol.tool_media import ToolMediaError
    request = LLMRequest(system=_PROBE_SYSTEM, messages=[dict(_PROBE_USER)],
                         tools=_PROBE_TOOLS, max_tokens=_probe_output_budget(model), stream=True)
    calls, text, finish = {}, [], None
    stream = iter(model.call_stream(request))
    try:
        for chunk in stream:
            if chunk.get('error'):
                raise ToolMediaError('capability_probe_call_failed')
            for choice in chunk.get('choices') or []:
                delta = choice.get('delta') or {}
                if delta.get('content'):
                    text.append(delta['content'])
                for item in delta.get('tool_calls') or []:
                    if item.get('type') not in (None, 'function'):
                        raise ToolMediaError('capability_probe_call_invalid')
                    index = item.get('index', 0)
                    call = calls.setdefault(index, dict(id='', name='', arguments=''))
                    call['id'] += item.get('id') or ''
                    function = item.get('function') or {}
                    call['name'] += function.get('name') or ''
                    call['arguments'] += function.get('arguments') or ''
                if choice.get('finish_reason'):
                    finish = choice['finish_reason']
    finally:
        if hasattr(stream, 'close'):
            stream.close()
    if len(calls) != 1 or finish != 'tool_calls':
        raise ToolMediaError('capability_probe_call_missing')
    call = next(iter(calls.values()))
    try:
        arguments = json.loads(call['arguments'])
    except (ValueError, TypeError):
        raise ToolMediaError('capability_probe_call_invalid') from None
    if not call['id'].strip() or call['name'] != 'read_probe' or arguments != {}:
        raise ToolMediaError('capability_probe_call_invalid')
    content = [dict(type='text', text=''.join(text))] if text else []
    content.append(dict(type='tool_use', id=call['id'], name=call['name'], input=arguments))
    return dict(role='assistant', content=content)


def capability_identity(model, revision):
    from models.config_revision import require_config_revision
    from models.openai.responses_adapter import use_responses_api
    require_config_revision(revision)
    bot = getattr(model, 'bot', None)
    if not hasattr(bot, 'get_api_config'):
        return None
    cfg = bot.get_api_config()
    name = getattr(model, 'model', '')
    resolve_provider = getattr(model, '_resolve_bot_type', None)
    provider = resolve_provider(name) if callable(resolve_provider) else getattr(model, 'bot_type', '')
    if not name or not provider or not use_responses_api(name, cfg.get('api_type')):
        return None
    return dict(model=name, endpoint_hash=hashlib.sha256(
        str(cfg.get('api_base') or '').rstrip('/').encode()).hexdigest(),
        provider=str(provider), protocol='responses',
        mime_type='application/pdf', transport='inline', config_revision=revision)


def _key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def ensure_pdf_capability(workspace, model, revision):
    """未知修订先探测；网络/限流/服务异常仍为unknown，下次允许再试。

    只支持实际 Responses function_call_output 内嵌 PDF，Files/URL 不推定。
    """
    from models.config_revision import use_config_revision
    with use_config_revision(revision):
        return _ensure_pdf_capability(workspace, model, revision)


def _ensure_pdf_capability(workspace, model, revision):
    identity = capability_identity(model, revision)
    if identity is None:
        return False
    service = SourceService(workspace)
    key = _key(identity)
    with service.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS reading_capabilities(id TEXT PRIMARY KEY,record TEXT)')
        row = db.execute('SELECT record FROM reading_capabilities WHERE id=?', (key,)).fetchone()
        if row:
            try:
                record = json.loads(row[0])
            except (TypeError, ValueError):
                record = None
            if (isinstance(record, dict) and type(record.get('schema_version')) is int
                    and record.get('schema_version') == 1
                    and record.get('probe_version') == 4 and record.get('identity') == identity
                    and record.get('fixture_profile') == _FIXTURE_PROFILE
                    and record.get('state') == 'supported' and record.get('verified_image') is True):
                return record['state'] == 'supported'
    # 图内随机数不在文字层/提问中，防止仅文字预处理被认作图文可用。
    from agent.protocol import LLMRequest
    from agent.protocol.tool_media import ToolMediaReference, ToolMediaContent, ToolMediaError, stream_with_tool_media
    from models.config_revision import require_config_revision
    require_config_revision(revision)
    code = str(secrets.randbelow(900000) + 100000)
    states = []

    def supports(context):
        return (capability_identity(model, revision) == identity
                and context.get('provider', identity['provider']) == identity['provider']
                and context.get('mime_type', identity['mime_type']) == identity['mime_type']
                and context.get('transport', identity['transport']) == identity['transport']
                and context.get('protocol') == 'responses' and context.get('model') == identity['model']
                and context.get('config_revision') == revision
                and hashlib.sha256(str(context.get('api_base') or '').rstrip('/').encode()).hexdigest()
                == identity['endpoint_hash'])

    def load(context):
        require_config_revision(revision)
        if not supports(context):
            raise ToolMediaError('attachment_unsupported')
        return ToolMediaContent('application/pdf', raw)

    ref = ToolMediaReference(load, supports, states.append)
    text, error = '', False
    stage, reason, exception_type = 'fixture', None, None
    error_code = None
    try:
        raw = _probe_pdf(code)
        stage = 'request_call'
        assistant = _request_probe_call(model)
        # 首轮后再次核验实际模型／Provider，不能借用其间变化的客户端。
        stage = 'verify_identity'
        if capability_identity(model, revision) != identity:
            reason = 'identity_changed'
            raise ToolMediaError('capability_probe_config_changed')
        stage = 'media_request'
        call = next(item for item in assistant['content'] if item.get('type') == 'tool_use')
        call_id = call['id']
        request = LLMRequest(system=_PROBE_SYSTEM,
            messages=[dict(_PROBE_USER), assistant, dict(role='user', content=[
                dict(type='tool_result', tool_use_id=call_id, content='本次能力测试图像')])],
            tools=_PROBE_TOOLS, max_tokens=_probe_output_budget(model), stream=True)
        for chunk in stream_with_tool_media(model, request, {call_id: (ref,)}):
            if chunk.get('error') and error_code in (None, 'unknown_error'):
                detail = chunk['error']
                candidate = detail.get('code') if type(detail) is dict else None
                error_code = (candidate if type(candidate) is str and candidate in _SAFE_ERROR_CODES
                              else 'unknown_error')
            error = error or bool(chunk.get('error'))
            for choice in chunk.get('choices') or []:
                text += choice.get('delta', {}).get('content') or ''
        if error:
            reason = 'stream_error'
    except Exception as exc:
        error = True
        # 仅此受信精确类型允许把完整字符串与固定码比对；绝不记录原文。
        candidate = str(exc) if type(exc) is ToolMediaError else None
        error_code = candidate if candidate in _SAFE_ERROR_CODES else 'unknown_error'
        if reason != 'identity_changed':
            reason = 'exception'
            # 只按精确类型查白名单；不读取异常详情、动态类名或服务端字段。
            exception_type = {
                TimeoutError: 'TimeoutError', ConnectionError: 'ConnectionError',
                OSError: 'OSError', ValueError: 'ValueError', TypeError: 'TypeError',
                ToolMediaError: 'ToolMediaError',
            }.get(type(exc), 'OtherError')
    # completed 后出现错误／unknown 也不能形成支持凭据。
    state = 'supported' if not error and states == ['sent', 'completed'] and text.strip() == code else 'unknown'
    if not error:
        stage = 'verify_result'
        reason = ('delivery_incomplete' if states != ['sent', 'completed'] else
                  'verified' if state == 'supported' else 'answer_mismatch')
    # 诊断与凭据隔离：不包含 identity、URL、路径、请求/回答内容或随机码。
    # 回执仅展示前四项固定枚举，总数用于识别异常序列，不能放宽支持判据。
    diagnostic = dict(stage=stage, reason=reason, exception_type=exception_type, error_code=error_code,
        delivery=[item if type(item) is str and item in ('sent', 'completed', 'unknown') else 'other'
                  for item in states[:4]], delivery_count=len(states),
        response_chars=len(text), exact_match=text.strip() == code)
    with service.transaction() as db:
        db.execute('INSERT OR REPLACE INTO reading_capabilities VALUES(?,?)', (key, json.dumps(
            dict(schema_version=1, probe_version=4, identity=identity, state=state,
                 fixture_profile=_FIXTURE_PROFILE,
                 verified_image=state == 'supported', diagnostic=diagnostic))))
    logger.info('PDF能力探测 %s', diagnostic)
    return state == 'supported'
