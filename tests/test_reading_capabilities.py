"""能力未知可重试，支持结论绑定实际配置，不外发用户资料。"""
import base64
from io import BytesIO
import json

import pytest

from agent.knowledge.reading_capabilities import ensure_pdf_capability, capability_identity
from models.config_revision import capture_config_revision, ConfigUnavailable


class ProbeBot:
    def get_api_config(self):
        return {'api_type': 'responses', 'api_base': 'https://offline.invalid/v1'}


class ProbeModel:
    model = 'offline-model'
    bot_type = 'custom:test'
    bot = ProbeBot()

    def call_stream(self, request):
        # 模拟首轮 API 流；第二轮仍由各测试的 stream_with_tool_media 验证。
        assert [t['name'] for t in request.tools] == ['read_probe']
        assert len(request.messages) == 1
        # 与实际主Agent保持同一模型预算；额外压低会造成部分网关PDF失败。
        assert request.max_tokens == getattr(self, 'expected_budget', None)
        yield {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_model_generated',
            'type': 'function', 'function': {'name': 'read_probe', 'arguments': '{}'}}]}}]}
        yield {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}


def test_unknown_failure_retries_and_success_is_cached(tmp_path, monkeypatch):
    calls = []
    def transport(model, request, pending):
        calls.append(request)
        assert next(iter(pending)) == 'call_model_generated'
        assert request.messages[1]['content'][-1]['id'] == 'call_model_generated'
        assert request.messages[2]['content'][0]['tool_use_id'] == 'call_model_generated'
        if len(calls) == 1:
            yield {'error': True}
            return
        ref = next(iter(pending.values()))[0]
        ref.on_delivery('sent')
        # 验证探针以PDF媒体交付；随机校验码不在提问文本中。
        context = dict(protocol='responses', model=model.model, api_base=model.bot.get_api_config()['api_base'],
                       config_revision=capture_config_revision())
        assert ref.supports(context)
        media = ref.loader(context)
        assert media.mime_type == 'application/pdf' and media.data.startswith(b'%PDF-')
        # 测试不做OCR：使用固定随机数替身，仅验证生命周期/能力缓存。
        ref.on_delivery('completed')
        yield {'choices': [{'delta': {'content': '472913'}}]}
    monkeypatch.setattr('secrets.randbelow', lambda n: 372913)
    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', transport)
    revision = capture_config_revision()
    model = ProbeModel()
    assert not ensure_pdf_capability(tmp_path, model, revision)
    assert ensure_pdf_capability(tmp_path, model, revision)
    assert ensure_pdf_capability(tmp_path, model, revision)
    assert len(calls) == 2
    assert '472913' not in str(calls[-1].messages)
    model.model = 'different-model'
    assert ensure_pdf_capability(tmp_path, model, revision)
    assert len(calls) == 3


def test_nonresponses_model_never_runs_probe(tmp_path):
    class ChatBot:
        def get_api_config(self):
            return {'api_type': 'chat'}
    model = ProbeModel()
    model.bot = ChatBot()
    assert capability_identity(model, capture_config_revision()) is None
    assert not ensure_pdf_capability(tmp_path, model, capture_config_revision())


@pytest.fixture
def probe_transport(monkeypatch):
    calls = []

    def transport(model, request, pending):
        ref = next(iter(pending.values()))[0]
        context = dict(protocol='responses', model=model.model,
                       api_base=model.bot.get_api_config()['api_base'],
                       config_revision=capture_config_revision())
        assert ref.supports(context)
        media = ref.loader(context)
        calls.append((request, context, ref, media))
        ref.on_delivery('sent')
        ref.on_delivery('completed')
        yield {'choices': [{'delta': {'content': '472913'}}]}

    monkeypatch.setattr('secrets.randbelow', lambda n: 372913)
    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', transport)
    return calls


@pytest.mark.parametrize('dimension', ['model', 'provider', 'endpoint', 'protocol', 'revision'])
def test_cache_identity_isolates_all_dimensions(tmp_path, monkeypatch, probe_transport, dimension):
    from config import Config, config_scope
    with config_scope(Config({'test_revision': 1})):
        revision = capture_config_revision()
        model = ProbeModel()
        assert ensure_pdf_capability(tmp_path, model, revision)
        first = capability_identity(model, revision)
        if dimension == 'model':
            model.model = 'other-model'
        elif dimension == 'provider':
            model.bot_type = 'custom:other'
        elif dimension in ('endpoint', 'protocol'):
            cfg = model.bot.get_api_config()
            cfg['api_base' if dimension == 'endpoint' else 'api_type'] = (
                'https://other.invalid/v1' if dimension == 'endpoint' else 'chat')
            monkeypatch.setattr(model.bot, 'get_api_config', lambda: cfg)
        if dimension == 'revision':
            with config_scope(Config({'test_revision': 2})):
                second_revision = capture_config_revision()
                assert ensure_pdf_capability(tmp_path, model, second_revision)
                assert capability_identity(model, second_revision) != first
        elif dimension == 'protocol':
            assert capability_identity(model, revision) is None
            assert not ensure_pdf_capability(tmp_path, model, revision)
        else:
            assert ensure_pdf_capability(tmp_path, model, revision)
            assert capability_identity(model, revision) != first
        assert len(probe_transport) == (1 if dimension == 'protocol' else 2)


def test_resolved_provider_overrides_primary_label(tmp_path, monkeypatch, probe_transport):
    model = ProbeModel()
    actual = ['custom:fallback-a']
    monkeypatch.setattr(model, '_resolve_bot_type', lambda name: actual[0], raising=False)
    revision = capture_config_revision()
    assert capability_identity(model, revision)['provider'] == actual[0]
    assert ensure_pdf_capability(tmp_path, model, revision)
    actual[0] = 'custom:fallback-b'
    assert ensure_pdf_capability(tmp_path, model, revision)
    assert len(probe_transport) == 2


def test_stale_revision_rejected_before_bot_access_and_cached_success(tmp_path, probe_transport):
    from config import Config, config_scope
    with config_scope(Config({'revision': 'old'})):
        revision = capture_config_revision()
        model = ProbeModel()
        assert ensure_pdf_capability(tmp_path, model, revision)
    class MustNotReadBot:
        @property
        def bot(self):
            pytest.fail('失效配置不应创建客户端或读取凭据')
    with config_scope(Config({'revision': 'new'})):
        for instance in (model, MustNotReadBot()):
            with pytest.raises(ConfigUnavailable):
                ensure_pdf_capability(tmp_path, instance, revision)
            with pytest.raises(ConfigUnavailable):
                capability_identity(instance, revision)
    assert len(probe_transport) == 1


@pytest.mark.parametrize('changed', [dict(protocol='chat'), dict(model='other'),
    dict(api_base='https://wrong.invalid/v1'), dict(config_revision='old'), dict(provider='custom:wrong'),
    dict(transport='files'), dict(transport='url'), dict(mime_type='image/png')])
def test_loader_itself_rejects_context_mismatch(tmp_path, probe_transport, changed):
    from agent.protocol.tool_media import ToolMediaError
    revision = capture_config_revision()
    assert ensure_pdf_capability(tmp_path, ProbeModel(), revision)
    _, context, ref, _ = probe_transport[0]
    context = dict(context, **changed)
    assert not ref.supports(context)
    with pytest.raises(ToolMediaError):
        ref.loader(context)


def test_loader_rejects_provider_switch_even_without_provider_in_context(tmp_path, probe_transport):
    from agent.protocol.tool_media import ToolMediaError
    model = ProbeModel()
    assert ensure_pdf_capability(tmp_path, model, capture_config_revision())
    _, context, ref, _ = probe_transport[0]
    model.bot_type = 'custom:switched'
    assert not ref.supports(context)
    with pytest.raises(ToolMediaError):
        ref.loader(context)


@pytest.mark.parametrize('failure', ['tls', 'timeout', 429, 500, 400, 'wrong_image', 'no_receipt', 'late_unknown'])
def test_failures_remain_unknown_and_retryable(tmp_path, monkeypatch, failure):
    from agent.knowledge.personal_assistant import SourceService
    calls = []

    def transport(model, request, pending):
        calls.append(True)
        ref = next(iter(pending.values()))[0]
        if failure == 'tls':
            raise ConnectionError('private endpoint TLS error')
        if failure == 'timeout':
            raise TimeoutError('private endpoint')
        if isinstance(failure, int):
            yield {'error': True, 'status_code': failure}
            return
        if failure != 'no_receipt':
            ref.on_delivery('sent')
            ref.on_delivery('completed')
        if failure == 'late_unknown':
            ref.on_delivery('unknown')
        yield {'choices': [{'delta': {'content': '000000' if failure == 'wrong_image' else '472913'}}]}

    monkeypatch.setattr('secrets.randbelow', lambda n: 372913)
    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', transport)
    revision = capture_config_revision()
    for _ in range(2):
        assert not ensure_pdf_capability(tmp_path, ProbeModel(), revision)
    assert len(calls) == 2
    with SourceService(tmp_path).transaction() as db:
        records = [json.loads(row[0]) for row in db.execute('SELECT record FROM reading_capabilities')]
    assert len(records) == 1 and records[0]['state'] == 'unknown'
    assert not records[0]['verified_image']
    assert 'private endpoint' not in json.dumps(records)


@pytest.mark.parametrize('record', ['broken-json', {'schema_version': 99, 'state': 'supported'},
    {'schema_version': 1, 'state': 'supported', 'verified_image': True},
    {'schema_version': 1, 'probe_version': 2, 'state': 'supported', 'verified_image': False}])
def test_invalid_or_obsolete_cached_proof_is_reprobed(tmp_path, record, probe_transport):
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.reading_capabilities import _key
    model = ProbeModel()
    revision = capture_config_revision()
    identity = capability_identity(model, revision)
    if isinstance(record, dict):
        record = json.dumps(dict(record, identity=identity))
    with SourceService(tmp_path).transaction() as db:
        db.execute('CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO reading_capabilities VALUES(?,?)', (_key(identity), record))
    assert ensure_pdf_capability(tmp_path, model, revision)
    assert len(probe_transport) == 1


def test_pdf_materials_reach_real_adapter_inline_with_mapping(tmp_path, monkeypatch):
    """真实 bot/adapter + HTTP 替身，不借用标准 Files，也不 JSON 化工具媒体。"""
    from pypdf import PdfReader
    from agent.protocol.tool_media import ToolMediaReference, ToolMediaContent, stream_with_tool_media
    from agent.knowledge.reading_materials import prepare_pdf_materials, load_pdf_materials
    from models.openai_compatible_bot import OpenAICompatibleBot
    import agent.knowledge.reading_capabilities as capabilities

    class Bot(OpenAICompatibleBot):
        get_api_config = ProbeBot.get_api_config

    class Model(ProbeModel):
        bot = Bot()

        def call_stream(self, request):
            yield from self.bot.call_with_tools(messages=request.messages, tools=request.tools,
                model=self.model, stream=True, max_tokens=request.max_tokens, system=request.system)

    http_payloads = []
    class Http:
        def responses(self, **kwargs):
            http_payloads.append(kwargs)
            if len(http_payloads) == 1:
                assert [t['name'] for t in kwargs['tools']] == ['read_probe']
                assert not any(i.get('type') == 'function_call_output' for i in kwargs['input'])
                yield {'type': 'response.output_text.delta', 'delta': '准备读取。'}
                yield {'type': 'response.output_item.added', 'output_index': 0,
                    'item': {'type': 'function_call', 'call_id': 'call_real_http', 'name': 'read_probe'}}
                yield {'type': 'response.function_call_arguments.delta', 'output_index': 0, 'delta': '{}'}
                yield {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}
                return
            outputs = [i for i in kwargs['input'] if i.get('type') == 'function_call_output']
            assert outputs[0]['call_id'] == 'call_real_http'
            assert any(i.get('role') == 'assistant' for i in kwargs['input'])
            files = [part for item in kwargs['input'] if item.get('type') == 'function_call_output'
                     for part in item['output'] if isinstance(part, dict) and part.get('type') == 'input_file']
            assert len(files) == 1
            assert 'file_id' not in files[0] and 'file_url' not in files[0]
            raw = base64.b64decode(files[0]['file_data'].split(',')[1])
            assert not PdfReader(BytesIO(raw)).pages[0].extract_text()
            yield {'type': 'response.output_text.delta', 'delta': '472913'}
            yield {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}

    model = Model()
    monkeypatch.setattr(model.bot, '_get_http_client', lambda: Http())
    monkeypatch.setattr('secrets.randbelow', lambda n: 372913)
    # 在真正交付前经过材料准备及可信 load；不替换媒体扩展或 adapter。
    def through_materials(model, request, pending):
        revision = capture_config_revision()
        old = next(iter(pending.values()))[0]
        context = dict(protocol='responses', model=model.model, config_revision=revision,
                       api_base=model.bot.get_api_config()['api_base'])
        original = tmp_path / 'synthetic.pdf'
        original.write_bytes(old.loader(context).data)
        materials = prepare_pdf_materials(tmp_path, 'synthetic.pdf', document_id='probe-doc',
            revision_id='probe-rev', parse_id='local-probe', config_revision=revision)
        assert materials.segments[0].page_map == (1,)
        assert materials.segments[0].text_pages == ('',)
        def load(actual):
            old.loader(actual)  # 保留探针自身的配置和实际目标核验。
            verified = load_pdf_materials(tmp_path, materials.to_dict(), expected_manifest_hash=materials.manifest_hash)
            return ToolMediaContent('application/pdf', (tmp_path / verified.segments[0].material_relpath).read_bytes())
        call_id = next(iter(pending))
        new = ToolMediaReference(load, old.supports, old.on_delivery)
        yield from stream_with_tool_media(model, request, {call_id: (new,)})

    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', through_materials)
    revision = capture_config_revision()
    assert capabilities.ensure_pdf_capability(tmp_path, model, revision)
    assert capabilities.ensure_pdf_capability(tmp_path, model, revision)
    assert len(http_payloads) == 2
    identity = capabilities.capability_identity(model, revision)
    assert identity['transport'] == 'inline' and identity['protocol'] == 'responses'


def test_first_round_collects_streamed_call_and_assistant_text():
    from agent.knowledge.reading_capabilities import _request_probe_call
    class Model:
        def call_stream(self, request):
            assert len(request.tools) == 1 and request.tools[0]['name'] == 'read_probe'
            yield {'choices': [{'delta': {'content': '先读取。', 'tool_calls': [
                {'index': 0, 'type': 'function', 'id': 'call_from_api',
                 'function': {'name': 'read_', 'arguments': '{'}}]}}]}
            yield {'choices': [{'delta': {'tool_calls': [
                {'index': 0, 'function': {'name': 'probe', 'arguments': '}'}}]}}]}
            yield {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}
    assistant = _request_probe_call(Model())
    assert assistant == {'role': 'assistant', 'content': [
        {'type': 'text', 'text': '先读取。'},
        {'type': 'tool_use', 'id': 'call_from_api', 'name': 'read_probe', 'input': {}}]}


@pytest.mark.parametrize('case', ['error', 'exception', 'no_call', 'multiple', 'truncated',
                                 'wrong_name', 'bad_arguments', 'missing_id', 'wrong_type'])
def test_first_round_failure_never_sends_pdf_and_records_unknown(tmp_path, monkeypatch, case):
    from agent.knowledge.personal_assistant import SourceService
    closed = []
    class Model(ProbeModel):
        def call_stream(self, request):
            try:
                if case == 'error':
                    yield {'error': {'message': 'private endpoint error'}}
                    return
                if case == 'exception':
                    raise TimeoutError('private endpoint error')
                if case != 'no_call':
                    item = dict(index=0, id='' if case == 'missing_id' else 'call_from_api',
                        type='custom' if case == 'wrong_type' else 'function', function=dict(
                        name='wrong' if case == 'wrong_name' else 'read_probe',
                        arguments='{' if case == 'bad_arguments' else '{}'))
                    calls = [item]
                    if case == 'multiple':
                        calls.append(dict(item, index=1, id='call_second'))
                    yield {'choices': [{'delta': {'tool_calls': calls}}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'length' if case == 'truncated' else 'tool_calls'}]}
            finally:
                closed.append(True)
    def must_not_send(*args):
        pytest.fail('首轮失败不得外发PDF或调用第二轮')
    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', must_not_send)
    assert not ensure_pdf_capability(tmp_path, Model(), capture_config_revision())
    assert closed == [True]
    with SourceService(tmp_path).transaction() as db:
        record = json.loads(db.execute('SELECT record FROM reading_capabilities').fetchone()[0])
    assert record['state'] == 'unknown' and record['probe_version'] == 4
    assert 'private endpoint' not in json.dumps(record)
    expected_code = ('capability_probe_call_failed' if case == 'error' else
                     'unknown_error' if case == 'exception' else
                     'capability_probe_call_missing' if case in ('no_call', 'multiple', 'truncated') else
                     'capability_probe_call_invalid')
    assert record['diagnostic']['error_code'] == expected_code


def test_synthetic_supported_proof_not_inherited(tmp_path, probe_transport):
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.reading_capabilities import _key
    model = ProbeModel()
    revision = capture_config_revision()
    identity = capability_identity(model, revision)
    record = dict(schema_version=1, probe_version=2, identity=identity,
                  state='supported', verified_image=True)
    with SourceService(tmp_path).transaction() as db:
        db.execute('CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO reading_capabilities VALUES(?,?)', (_key(identity), json.dumps(record)))
    assert ensure_pdf_capability(tmp_path, model, revision)
    assert len(probe_transport) == 1


def test_fixture_matches_p01_geometry_and_image_content():
    from pypdf import PdfReader
    from agent.knowledge.reading_capabilities import _probe_pdf
    raw = _probe_pdf('472913')
    assert raw.startswith(b'%PDF-') and len(raw) <= 128 * 1024
    reader = PdfReader(BytesIO(raw))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert float(page.mediabox.width) == pytest.approx(540)
    assert float(page.mediabox.height) == pytest.approx(360)
    assert not page.extract_text()
    image = page.images[0].image.convert('RGB')
    assert image.size == (900, 600)
    for x in (150, 450, 750):
        r, g, b = image.getpixel((x, 270))
        assert b > 200 and r < 30 and g < 30
    for x in (300, 600):
        r, g, b = image.getpixel((x, 480))
        assert r > 200 and g < 30 and b < 30
    # 数字区存在像素且随机数变化会改变图像；不靠 PDF 文字层传答案。
    other = PdfReader(BytesIO(_probe_pdf('628145'))).pages[0].images[0].image
    assert image.crop((280, 35, 600, 120)).tobytes() != other.crop((280, 35, 600, 120)).tobytes()


def test_missing_fixture_font_fails_before_any_model_call(tmp_path, monkeypatch):
    from agent.knowledge.personal_assistant import SourceService
    def missing(*args, **kwargs):
        raise OSError('font unavailable')
    monkeypatch.setattr('PIL.ImageFont.truetype', missing)
    model = ProbeModel()
    monkeypatch.setattr(model, 'call_stream', lambda request: pytest.fail('缺字体不得付费'))
    assert not ensure_pdf_capability(tmp_path, model, capture_config_revision())
    with SourceService(tmp_path).transaction() as db:
        record = json.loads(db.execute('SELECT record FROM reading_capabilities').fetchone()[0])
    assert record['state'] == 'unknown'
    assert record['fixture_profile'] == 'p01-900x600-arial64-shapes-120dpi-v1'


def test_small_fixture_supported_proof_not_inherited(tmp_path, probe_transport):
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.reading_capabilities import _key
    model = ProbeModel()
    revision = capture_config_revision()
    identity = capability_identity(model, revision)
    record = dict(schema_version=1, probe_version=3, identity=identity,
                  state='supported', verified_image=True)
    with SourceService(tmp_path).transaction() as db:
        db.execute('CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO reading_capabilities VALUES(?,?)', (_key(identity), json.dumps(record)))
    assert ensure_pdf_capability(tmp_path, model, revision)
    assert len(probe_transport) == 1


@pytest.mark.parametrize('case,stage,reason,exception_type,delivery,chars,match', [
    ('fixture', 'fixture', 'exception', 'OSError', [], 0, False),
    ('first_error', 'request_call', 'exception', 'ToolMediaError', [], 0, False),
    ('first_timeout', 'request_call', 'exception', 'TimeoutError', [], 0, False),
    ('identity', 'verify_identity', 'identity_changed', None, [], 0, False),
    ('second_error', 'media_request', 'stream_error', None, ['sent', 'unknown'], 0, False),
    ('second_exception', 'media_request', 'exception', 'ConnectionError', ['sent'], 2, False),
    ('custom_exception', 'media_request', 'exception', 'OtherError', ['sent'], 0, False),
    ('mismatch', 'verify_result', 'answer_mismatch', None, ['sent', 'completed'], 6, False),
    ('no_delivery', 'verify_result', 'delivery_incomplete', None, [], 6, True),
    ('late_unknown', 'verify_result', 'delivery_incomplete', None, ['sent', 'completed', 'unknown'], 6, True),
    ('unsafe_delivery', 'verify_result', 'delivery_incomplete', None, ['sent', 'completed', 'other'], 6, True),
    ('success', 'verify_result', 'verified', None, ['sent', 'completed'], 8, True),
])
def test_safe_diagnostic_locates_probe_failure(tmp_path, monkeypatch, case, stage, reason,
                                            exception_type, delivery, chars, match):
    from agent.knowledge.personal_assistant import SourceService
    import agent.knowledge.reading_capabilities as capabilities
    # 同时污染异常名、详情、流式错误、答案及回执；只允许固定枚举进入诊断。
    from common.log import logger
    secret = 'https://secret.invalid/token D:/private/document.pdf payload-secret'
    unsafe_error = type('PrivatePayloadSecret', (Exception,), {})
    logs, rounds = [], []
    monkeypatch.setattr(logger, 'info', lambda *args, **kwargs: logs.append((args, kwargs)))
    monkeypatch.setattr('secrets.randbelow', lambda n: 372913)

    def fixture(code):
        if case == 'fixture':
            raise OSError(secret)
        return b'%PDF-test'

    class Model(ProbeModel):
        def call_stream(self, request):
            rounds.append('first')
            if case == 'first_error':
                yield {'error': {'message': secret, 'code': secret}}
                return
            if case == 'first_timeout':
                raise TimeoutError(secret)
            yield from super().call_stream(request)
            if case == 'identity':
                self.model = 'changed'

    def transport(model, request, pending):
        rounds.append('second')
        ref = next(iter(pending.values()))[0]
        if case == 'no_delivery':
            yield {'choices': [{'delta': {'content': '472913'}}]}
            return
        ref.on_delivery('sent')
        if case == 'second_error':
            ref.on_delivery('unknown')
            yield {'error': {'message': secret, 'code': secret}, 'payload': secret}
            return
        if case == 'second_exception':
            yield {'choices': [{'delta': {'content': '47'}}]}
            raise ConnectionError(secret)
        if case == 'custom_exception':
            raise unsafe_error(secret)
        ref.on_delivery('completed')
        if case in ('late_unknown', 'unsafe_delivery'):
            ref.on_delivery('unknown' if case == 'late_unknown' else secret)
        yield {'choices': [{'delta': {'content': '628145' if case == 'mismatch' else ' 472913 ' if case == 'success' else '472913'}}]}

    monkeypatch.setattr(capabilities, '_probe_pdf', fixture)
    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', transport)
    assert ensure_pdf_capability(tmp_path, Model(), capture_config_revision()) == (case == 'success')
    with SourceService(tmp_path).transaction() as db:
        record = json.loads(db.execute('SELECT record FROM reading_capabilities').fetchone()[0])
    diagnostic = record['diagnostic']
    error_code = (dict(first_error='capability_probe_call_failed', identity='capability_probe_config_changed')
                  .get(case, 'unknown_error') if stage != 'verify_result' else None)
    assert diagnostic == dict(stage=stage, reason=reason, exception_type=exception_type,
                              error_code=error_code,
                              delivery=delivery, delivery_count=len(delivery),
                              response_chars=chars, exact_match=match)
    assert logs == [(('PDF能力探测 %s', diagnostic), {})]
    assert {'identity', 'endpoint', 'endpoint_hash', 'api_base', 'payload', 'message'}.isdisjoint(diagnostic)
    encoded = json.dumps(logs, ensure_ascii=False)
    for forbidden in (secret, 'PrivatePayloadSecret', '472913', '628145', 'offline.invalid',
                      'endpoint_hash', 'payload', 'call_model_generated'):
        assert forbidden not in encoded
    assert secret not in json.dumps(record)
    assert rounds == ([] if case == 'fixture' else ['first'] if stage in ('request_call', 'verify_identity')
                      else ['first', 'second'])


def test_historical_v4_supported_without_diagnostic_is_reused(tmp_path, monkeypatch):
    from agent.knowledge.personal_assistant import SourceService
    import agent.knowledge.reading_capabilities as capabilities
    model = ProbeModel()
    revision = capture_config_revision()
    identity = capability_identity(model, revision)
    record = dict(schema_version=1, probe_version=4, identity=identity, state='supported',
                  verified_image=True, fixture_profile=capabilities._FIXTURE_PROFILE)
    with SourceService(tmp_path).transaction() as db:
        db.execute('CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO reading_capabilities VALUES(?,?)', (capabilities._key(identity), json.dumps(record)))
    monkeypatch.setattr(capabilities, '_probe_pdf', lambda code: pytest.fail('历史凭据不得重新探测'))
    assert ensure_pdf_capability(tmp_path, model, revision)
    with SourceService(tmp_path).transaction() as db:
        assert json.loads(db.execute('SELECT record FROM reading_capabilities').fetchone()[0]) == record


def test_diagnostic_bounds_delivery_and_never_records_answer(tmp_path, monkeypatch):
    from agent.knowledge.personal_assistant import SourceService
    import agent.knowledge.reading_capabilities as capabilities
    from common.log import logger
    answer = 'https://private.invalid D:/private/answer.pdf payload-secret 472913'
    logged = []
    monkeypatch.setattr(logger, 'info', lambda message, diagnostic: logged.append(diagnostic))
    monkeypatch.setattr(capabilities, '_probe_pdf', lambda code: b'%PDF-test')
    def transport(model, request, pending):
        ref = next(iter(pending.values()))[0]
        for state in ['sent', 'completed'] + [answer] * 20:
            ref.on_delivery(state)
        yield {'choices': [{'delta': {'content': answer}}]}
    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', transport)
    assert not ensure_pdf_capability(tmp_path, ProbeModel(), capture_config_revision())
    with SourceService(tmp_path).transaction() as db:
        record = json.loads(db.execute('SELECT record FROM reading_capabilities').fetchone()[0])
    diagnostic = record['diagnostic']
    assert diagnostic['delivery'] == ['sent', 'completed', 'other', 'other']
    assert diagnostic['delivery_count'] == 22
    assert diagnostic['response_chars'] == len(answer)
    assert diagnostic['exact_match'] is False
    assert diagnostic['reason'] == 'delivery_incomplete'
    assert logged == [diagnostic]
    assert answer not in json.dumps(record)
    assert answer not in json.dumps(logged)


@pytest.mark.parametrize('origin,code,expected', [
    ('exception', 'capability_probe_call_failed', 'capability_probe_call_failed'),
    ('exception', 'capability_probe_call_missing', 'capability_probe_call_missing'),
    ('exception', 'capability_probe_call_invalid', 'capability_probe_call_invalid'),
    ('exception', 'capability_probe_config_changed', 'capability_probe_config_changed'),
    ('exception', 'capability_probe_call_failed https://secret.invalid private-payload', 'unknown_error'),
    ('subclass', 'capability_probe_call_missing', 'unknown_error'),
    ('chunk', 'attachment_unsupported', 'attachment_unsupported'),
    ('chunk', 'media_context_lost', 'media_context_lost'),
    ('chunk', 'media_request_timeout', 'media_request_timeout'),
    ('chunk', 'attachment_unsupported https://secret.invalid private-payload', 'unknown_error'),
    ('chunk', {'message': 'private-payload'}, 'unknown_error'),
])
def test_safe_error_code_allowlist(tmp_path, monkeypatch, origin, code, expected):
    from agent.knowledge.personal_assistant import SourceService
    from agent.protocol.tool_media import ToolMediaError
    from common.log import logger
    import agent.knowledge.reading_capabilities as capabilities
    logs = []
    monkeypatch.setattr(logger, 'info', lambda message, diagnostic: logs.append(diagnostic))
    monkeypatch.setattr(capabilities, '_probe_pdf', lambda code: b'%PDF-test')
    class UnsafeSubclass(ToolMediaError):
        pass
    def first(model):
        raise (UnsafeSubclass if origin == 'subclass' else ToolMediaError)(code)
    def transport(model, request, pending):
        yield {'error': {'code': code, 'message': 'https://secret.invalid private-payload'},
               'payload': 'private-payload'}
    if origin in ('exception', 'subclass'):
        monkeypatch.setattr(capabilities, '_request_probe_call', first)
    monkeypatch.setattr('agent.protocol.tool_media.stream_with_tool_media', transport)
    assert not ensure_pdf_capability(tmp_path, ProbeModel(), capture_config_revision())
    with SourceService(tmp_path).transaction() as db:
        record = json.loads(db.execute('SELECT record FROM reading_capabilities').fetchone()[0])
    diagnostic = record['diagnostic']
    assert diagnostic['error_code'] == expected
    assert diagnostic['stage'] == ('media_request' if origin == 'chunk' else 'request_call')
    assert logs == [diagnostic]
    for forbidden in ('secret.invalid', 'private-payload', 'UnsafeSubclass'):
        assert forbidden not in json.dumps(record)
        assert forbidden not in json.dumps(logs)
