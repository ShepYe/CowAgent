"""个人资料任务入口；身份和原件路径均由受信入口决定。"""
import hashlib
import json
import mimetypes
import tempfile
from pathlib import Path
from urllib.parse import quote

import web

from channel.web.core._common import (
    _require_auth, _desktop_token_matches, _multipart_lists, _first_value,
    _read_uploaded_file_bytes_limited,
    _get_workspace_root,
)
from common.personal_tasks import TaskStore, TaskError, OWNER

MAX_FILE = 100 * 1024 * 1024


def _reply(**data):
    web.header('Content-Type', 'application/json; charset=utf-8')
    return json.dumps(data, ensure_ascii=False)


def _error(exc, task=None):
    code = getattr(exc, 'code', 'storage_failed' if isinstance(exc, OSError) else 'invalid_request')
    # 不回显异常中的本机路径或配置内容。
    return _reply(status='error', code=code, message={
        'version_conflict': '任务或来源状态已改变，请刷新',
        'forbidden': '对象不存在或无权访问',
        'storage_failed': '保存失败，请检查本机存储',
        'idempotency_conflict': '提交标识与已有内容冲突',
        'limit_exceeded': '文件超过 100 MiB 上限',
        'pairing_unavailable': '绑定码已失效或机器人配置已改变，请重新开始绑定',
        'channel_unavailable': '请先在渠道页配置并连接飞书机器人',
        'websocket_required': '引导绑定需要飞书长连接模式',
        'config_unavailable': '原任务配置已不可用，请按当前配置重新处理',
    }.get(code, '请求无法完成'), task=task)


def _body():
    body = json.loads(web.data() or b'{}')
    if not isinstance(body, dict) or 'owner' in body or 'owner_id' in body:
        raise TaskError('invalid_request')
    return body


def _source_service():
    from agent.knowledge.personal_assistant import SourceService
    return SourceService(_get_workspace_root(), owner_id=OWNER)


def _wiki_service():
    from agent.knowledge.wiki import WikiService
    return WikiService(_get_workspace_root(), owner_id=OWNER)


def _task_source_refs(task):
    """授权只来自本任务留存的回执，不扩展至其他任务或整份知识库。"""
    return [source for ref in task.get('result_refs', []) for source in [ref, *ref.get('sources', [])]]


def _display_parse(source, service, cache):
    """视觉展示使用本修订最佳离线解析；引用的不可变 parse_id 不变。"""
    key = (source.get('document_id'), source.get('revision_id'))
    if key not in cache:
        parse = getattr(service, 'parse', None)
        parsed = parse(source['revision_id']) if callable(parse) else None
        if not isinstance(parsed, dict):
            parsed = json.loads((service.root / 'parses' / (source['parse_id'] + '.json')).read_text(encoding='utf-8'))
        cache[key] = parsed
    return cache[key]


def _decorate_source(source, service, parse_cache):
    source = dict(source, source_state=None, source_status='unavailable')
    try:
        state = service.get(source['document_id'])
        revision = service.registered_revision(source['document_id'], source.get('revision_id'))
        latest = service.registered_revision(source['document_id'])
        status = state['validity'] if state['validity'] != 'active' else (
            'transient' if state['purpose'] != 'long_term' else (
                'superseded_or_changed' if latest['revision_id'] != revision['revision_id'] or
                source.get('source_state_snapshot', state) != state else 'current'))
        source.update(source_state=state, source_status=status, filename=revision['filename'],
                      title=Path(revision['filename'].replace('\\', '/')).stem or '资料原文')
        if source.get('parse_id'):
            parsed = _display_parse(source, service, parse_cache)
            source['visual_coverage'] = parsed.get('visual_coverage', [])
            source['visual_warnings'] = parsed.get('visual_warnings', [])
    except (ValueError, OSError):
        pass
    return source


def _decorate(task):
    refs = []
    parse_cache = {}
    for ref in task.get('result_refs', []):
        ref = dict(ref)
        ref['current_searchable'] = False
        if ref.get('document_id'):
            try:
                service = _source_service()
                if ref.get('sources'):
                    ref['sources'] = [_decorate_source(source, service, parse_cache) for source in ref['sources']]
                ref['source_state'] = service.get(ref['document_id'])
                if ref.get('page_id'):
                    current = _wiki_service().current(ref['page_id'])
                    ref['current_searchable'] = bool(current and current.get('searchable') is True and current.get('publish_id') == ref.get('publish_id'))
                    if ref['current_searchable']:
                        ref.update({key: current[key] for key in ('category', 'category_mode', 'category_revision')})
                else:
                    current = service.current_publication(ref['document_id'], ref.get('revision_id'))
                    if current and current.get('publish_id') == ref.get('publish_id') and current.get('searchable') is True:
                        parsed = json.loads((service.root / 'parses' / (current['parse_id'] + '.json')).read_text(encoding='utf-8'))
                        with service.transaction() as db:
                            rows = db.execute('SELECT text,location FROM keyword_units WHERE publication=? ORDER BY ordinal', (current['publish_id'],)).fetchall()
                        ref['current_searchable'] = bool(rows) and [(r[0], json.loads(r[1])) for r in rows] == [(u['text'], u['location']) for u in parsed['units']]
                revision = service.registered_revision(ref['document_id'], ref.get('revision_id'))
                if not ref.get('page_id') or not isinstance(ref.get('title'), str) or not ref['title'].strip():
                    ref['title'] = Path(revision['filename'].replace('\\', '/')).stem or '资料原文'
                if not ref.get('page_id') or not ref.get('category'):
                    ref['category'] = '资料原文' if not ref.get('page_id') else '未分类'
                ref['filename'] = revision['filename']
                coverage = []
                warnings = []
                seen_revisions = set()
                for source in ref.get('sources') or [ref]:
                    key = (source.get('document_id'), source.get('revision_id'))
                    if source.get('parse_id') and key not in seen_revisions:
                        seen_revisions.add(key)
                        parsed = _display_parse(source, service, parse_cache)
                        coverage.extend(dict(page, filename=source.get('filename', revision['filename'])) for page in parsed.get('visual_coverage', []))
                        warnings.extend(parsed.get('visual_warnings', []))
                ref['visual_coverage'] = coverage
                ref['visual_warnings'] = list({json.dumps(warning, ensure_ascii=False, sort_keys=True): warning for warning in warnings}.values())
            except (ValueError, OSError):
                ref['source_state'] = None
                ref['current_searchable'] = False
        refs.append(ref)
    return {**task, 'result_refs': refs, 'attachments': _decorate_attachments(task)}


def _decorate_attachments(task):
    """逻辑身份来自受信登记决定，字节校验只验证该关联。"""
    attachments = []
    for raw in task.get('attachments', []):
        item = {k: v for k, v in raw.items() if k not in ('source_document_id', 'source_revision_id')}
        try:
            decision = (task.get('attachment_decisions') or {}).get(item['attachment_id']) or {}
            binding = decision.get('source')
            if not binding and len(task['attachments']) == 1:
                binding = (task.get('payload') or {}).get('source_action') or (task.get('payload') or {}).get('reorganize')
            if binding:
                service = _source_service()
                if decision.get('workspace') and Path(decision['workspace']).resolve() != service.workspace:
                    raise ValueError('来源工作区不符')
                revision = service.registered_revision(binding['document_id'], binding['revision_id'])
                digest = revision['sha256']
                if (hashlib.sha256(Path(revision['original']).read_bytes()).hexdigest() != digest
                        or item['sha256'] != digest or hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != digest):
                    raise ValueError('原件核验失败')
                item.update(source_document_id=binding['document_id'], source_revision_id=binding['revision_id'])
        except (ValueError, OSError, KeyError, TypeError):
            pass
        attachments.append(item)
    return attachments


class PersonalTasksHandler:
    def GET(self):
        _require_auth()
        try:
            return _reply(status='success', tasks=[_decorate(t) for t in TaskStore().list(OWNER)])
        except (ValueError, OSError) as exc:
            return _error(exc)

    def POST(self):
        _require_auth()
        try:
            if int(web.ctx.env.get('CONTENT_LENGTH') or 0) > MAX_FILE + 1024 * 1024:
                raise TaskError('limit_exceeded')
            params = _multipart_lists(16)
            if 'owner' in params or 'owner_id' in params or 'local_path' in params:
                raise TaskError('invalid_request')
            files = params.get('file') or []
            request_id = _first_value(params, 'request_id', '')
            text = _first_value(params, 'text', '')
            if len(files) != 1 or not isinstance(request_id, str) or not request_id or len(request_id) > 200:
                raise TaskError('invalid_request')
            uploaded = files[0]
            filename = Path((getattr(uploaded, 'filename', '') or '').replace('\\', '/')).name
            if Path(filename).suffix.lower() not in {'.pdf', '.docx', '.xlsx', '.md', '.txt'}:
                raise TaskError('invalid_request')
            content = _read_uploaded_file_bytes_limited(uploaded, MAX_FILE)
            if len(content) > MAX_FILE:
                raise TaskError('limit_exceeded')
            from models.config_revision import capture_config_revision
            store = TaskStore()
            task = store.submit(OWNER, request_id,
                                {'text': text, 'filename': filename, 'sha256': hashlib.sha256(content).hexdigest()},
                                {'channel': 'web', 'channel_instance_id': 'desktop'}, capture_config_revision())
            with tempfile.TemporaryDirectory(prefix='cow-personal-') as temp:
                path = Path(temp) / 'upload'
                path.write_bytes(content)
                store.attach(OWNER, task['task_id'], str(path), filename, 'file-0')
            task = store.get(OWNER, task['task_id'])
            if task['status'] == 'queued':
                from common.personal_tasks import enqueue
                enqueue(task['task_id'])
            return _reply(status='success', task=task)
        except (ValueError, OSError) as exc:
            return _error(exc)


class PersonalTaskHandler:
    def GET(self, task_id):
        _require_auth()
        try:
            store = TaskStore()
            return _reply(status='success', task=_decorate(store.get(OWNER, task_id)), history=store.history(OWNER, task_id))
        except (ValueError, OSError) as exc:
            return _error(exc)

    def POST(self, task_id):
        _require_auth()
        store = TaskStore()
        try:
            body = _body()
            attempt = body.get('attempt')
            if type(attempt) is not int or attempt < 1:
                raise TaskError('invalid_request')
            action = body.get('action')
            if action == 'cancel':
                task = store.cancel(OWNER, task_id, attempt)
            elif action == 'retry':
                task = store.retry(OWNER, task_id, attempt)
            elif action == 'resubmit':
                task = _resubmit(store, task_id, attempt, body.get('request_id'))
            elif action == 'reorganize':
                task = _reorganize(store, task_id, attempt, body)
            elif action == 'update_input':
                task = store.update_input(OWNER, task_id, attempt, body.get('input'))
            elif action in {'withdraw', 'correct', 'change_purpose', 'republish'}:
                task = store.get(OWNER, task_id)
                document_id = body.get('document_id')
                if task['attempt'] != attempt:
                    raise TaskError('version_conflict')
                source_ref = next((r for r in _task_source_refs(task) if r.get('document_id') == document_id), None)
                if not source_ref:
                    raise TaskError('forbidden')
                state = body.get('expected_state')
                if type(state) is not int or state < 0:
                    raise TaskError('invalid_request')
                reason = body.get('reason', '本人从桌面撤回' if action == 'withdraw' else '')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 10000:
                    raise TaskError('invalid_request')
                service = _source_service()
                if action == 'republish':
                    task = _republish(store, task, source_ref, state, reason.strip(), body.get('request_id'), service)
                else:
                    purpose = body.get('purpose') if action == 'change_purpose' else None
                    if action == 'change_purpose' and purpose not in {'long_term', 'transient'}:
                        raise TaskError('invalid_request')
                    service.change(document_id, action='purpose' if action == 'change_purpose' else action,
                                   purpose=purpose, expected_state=state, reason=reason.strip())
            else:
                raise TaskError('invalid_request')
            if task['status'] == 'queued':
                from common.personal_tasks import enqueue
                enqueue(task['task_id'])
            return _reply(status='success', task=_decorate(task))
        except (ValueError, OSError) as exc:
            latest = None
            if getattr(exc, 'code', '') == 'version_conflict':
                latest = store.get(OWNER, task_id)
            return _error(exc, latest)


def _resubmit(store, task_id, attempt, request_id):
    """Explicit new admission; the failed task never switches configuration."""
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        raise TaskError('invalid_request')
    from models.config_revision import capture_config_revision
    revision = capture_config_revision()
    # Keep dispatch/recovery from seeing the child before all attachments are
    # copied. Verified blobs already exist; no new user-controlled path is read.
    with store.lock, store._transaction() as db:
        parent = store._read(db, OWNER, task_id)
        if parent['attempt'] != attempt:
            raise TaskError('version_conflict')
        if (parent['status'] != 'failed' or (parent.get('error') or {}).get('code') != 'config_unavailable' or
                not parent['attachments'] or parent['result_refs'] or parent.get('source_candidates') or
                'source_action' in parent['payload']):
            raise TaskError('invalid_request')
        # TaskStore.attachment opens another transaction; validate equivalent
        # checks here while holding the admission transaction instead.
        attachments = []
        for item in parent['attachments']:
            path = Path(item['path']).resolve()
            if (path.parent != (store.root / 'originals').resolve() or
                    hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']):
                raise TaskError('storage_failed')
            attachments.append(dict(item))
        import copy
        payload = copy.deepcopy(parent['payload'])
        if parent.get('clarification'):
            payload['text'] = payload.get('text', '') + '\n' + parent['clarification']
        origin = dict(parent['origin'], parent_task_id=task_id)
        # Prefix the client id with the parent/attempt so it cannot collide
        # with uploads or another failed task's submission.
        key = f'resubmit:{task_id}:{attempt}:{request_id}'
        fingerprint = hashlib.sha256(json.dumps({'payload': payload, 'origin': origin},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        old = db.execute('SELECT fingerprint,record FROM tasks WHERE owner=? AND request_id=?', (OWNER, key)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise TaskError('idempotency_conflict')
            return json.loads(old[1])
        from common.personal_tasks import now
        from uuid import uuid4
        task = dict(schema_version=1, receipt_id=uuid4().hex, task_id=uuid4().hex, request_id=key,
            owner_id=OWNER, origin=origin, payload=payload, status='queued', stage='receive', attempt=1,
            config_revision=revision, attachments=attachments, result_refs=[], outcome=None, error=None,
            notification='pending', created_at=now(), updated_at=now())
        if parent.get('download_complete'):
            task['download_complete'] = True
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)', (task['task_id'], OWNER, key, fingerprint,
            json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(',', ':'))))
        return store._save(db, task)


def _reorganize(store, task_id, attempt, body):
    """复用受信原件；任务与附件一次提交，旧知识在新发布成功前继续有效。"""
    request_id, state = body.get('request_id'), body.get('expected_state')
    if (not isinstance(request_id, str) or not request_id or len(request_id) > 200 or
            type(state) is not int or state < 0):
        raise TaskError('invalid_request')
    service = _source_service()
    from models.config_revision import capture_config_revision
    from common.personal_tasks import now
    from uuid import uuid4
    revision_config = capture_config_revision()
    with service.transaction() as source_db, store._transaction() as db:
        parent = store._read(db, OWNER, task_id)
        if parent['attempt'] != attempt:
            raise TaskError('version_conflict')
        document_id = body.get('document_id')
        # 新页可以有多个来源；授权仍必须来自当前父任务的回执。
        refs = _task_source_refs(parent)
        ref = next((r for r in refs if r.get('document_id') == document_id), None)
        if not ref:
            raise TaskError('forbidden')
        directive = dict(document_id=document_id, revision_id=ref.get('revision_id'), expected_state=state)
        key = f'reorganize:{task_id}:{attempt}:{request_id}'
        fingerprint = hashlib.sha256(json.dumps(directive, sort_keys=True).encode()).hexdigest()
        previous = db.execute('SELECT fingerprint,record FROM tasks WHERE owner=? AND request_id=?', (OWNER, key)).fetchone()
        if previous:
            if previous[0] != fingerprint:
                raise TaskError('idempotency_conflict')
            return json.loads(previous[1])
        doc = service._document(source_db, document_id)
        if doc['state'] != state:
            raise TaskError('version_conflict')
        if doc['purpose'] != 'long_term' or doc['validity'] != 'active':
            raise TaskError('forbidden')
        row = source_db.execute('SELECT record FROM revisions WHERE id=? AND document=?',
                                (directive['revision_id'], document_id)).fetchone()
        if not row:
            raise TaskError('forbidden')
        revision = json.loads(row[0])
        latest = source_db.execute('SELECT id FROM revisions WHERE document=? ORDER BY rowid DESC LIMIT 1', (document_id,)).fetchone()
        if latest[0] != directive['revision_id']:
            raise TaskError('version_conflict')
        if hashlib.sha256(Path(revision['original']).read_bytes()).hexdigest() != revision['sha256']:
            raise TaskError('storage_failed')
        if doc['current'] and doc['current'] != directive['revision_id']:
            raise TaskError('version_conflict')
        attachments = []
        for attachment in parent['attachments']:
            if attachment['sha256'] != revision['sha256']:
                continue
            path = Path(attachment['path']).resolve()
            if (path.parent != (store.root / 'originals').resolve() or
                    hashlib.sha256(path.read_bytes()).hexdigest() != revision['sha256']):
                raise TaskError('storage_failed')
            attachments.append(dict(attachment))
        if not attachments:
            raise TaskError('forbidden')
        payload = {'reorganize': directive, 'text':
            '请重新整理此已登记长期资料。先读取 knowledge-wiki 技能、原附件和已有相关知识，'
            '根据真实证据提炼、命名、分类并提交主题知识页；调用 knowledge_ingest，保留下述 document_id、revision_id，'
            '核对 expected_state。不得以原文拼接代替整理，不调用 knowledge_source_action republish，'
            '不得改变临时或撤回权限。新发布失败时保留旧有效知识。\n' + json.dumps(directive, ensure_ascii=False)}
        task = dict(schema_version=1, receipt_id=uuid4().hex, task_id=uuid4().hex, request_id=key,
                    owner_id=OWNER, origin={'channel': 'web', 'channel_instance_id': 'desktop', 'parent_task_id': task_id},
                    payload=payload, status='queued', stage='receive', attempt=1, config_revision=revision_config,
                    attachments=attachments, download_complete=True, result_refs=[], outcome=None, error=None,
                    notification='pending', created_at=now(), updated_at=now())
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)', (task['task_id'], OWNER, key, fingerprint, json.dumps(task, ensure_ascii=False)))
        return store._save(db, task)


def _republish(store, parent, ref, state, reason, request_id, service):
    """显式新任务；原件只能来自父任务，状态授权快照交给 worker 再核验。"""
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        raise TaskError('invalid_request')
    document_id, revision_id = ref['document_id'], ref.get('revision_id')
    with service.transaction() as db:
        doc = service._document(db, document_id)
        if doc['state'] != state:
            raise TaskError('version_conflict')
        row = db.execute('SELECT record FROM revisions WHERE id=? AND document=?', (revision_id, document_id)).fetchone()
        if not row:
            raise TaskError('forbidden')
        revision = json.loads(row[0])
    selected = [store.attachment(OWNER, parent['task_id'], a['attachment_id']) for a in parent['attachments']
                if a['sha256'] == revision['sha256']]
    if not selected:
        raise TaskError('forbidden')
    from models.config_revision import capture_config_revision
    directive = dict(document_id=document_id, revision_id=revision_id, expected_state=state,
                     action='republish', purpose='long_term', reason=reason)
    task = store.submit(OWNER, request_id, {
        'text': '本人明确要求重新长期发布此资料。先按接收时 expected_state 核验来源状态，再调用 knowledge_source_action '
                'republish，随后 knowledge_ingest 使用同一 document_id、long_term 和 expected_current_revision=null。'
                '状态冲突立即停止，禁止改用新状态覆盖。依据：' + reason + '\n' + json.dumps(directive, ensure_ascii=False),
        'source_action': directive,
    }, {'channel': 'web', 'channel_instance_id': 'desktop', 'parent_task_id': parent['task_id']}, capture_config_revision())
    store.attach(OWNER, task['task_id'], selected[0]['path'], selected[0]['filename'], 'file-0')
    return store.get(OWNER, task['task_id'])


class PersonalOriginalHandler:
    def GET(self, task_id, attachment_id):
        _require_auth()
        try:
            item = TaskStore().attachment(OWNER, task_id, attachment_id)
            web.header('Content-Type', mimetypes.guess_type(item['filename'])[0] or 'application/octet-stream')
            web.header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(item['filename'], safe=''))
            web.header('X-Content-Type-Options', 'nosniff')
            return Path(item['path']).read_bytes()
        except (ValueError, OSError) as exc:
            return _error(exc)


class PersonalReferenceHandler:
    def GET(self, task_id, publish_id):
        _require_auth()
        try:
            task = TaskStore().get(OWNER, task_id)
            if not any(ref.get('publish_id') == publish_id for ref in task['result_refs']):
                raise TaskError('forbidden')
            ordinal = int(web.input(ordinal='0').ordinal)
            if ordinal < 0:
                raise TaskError('invalid_request')
            service = _source_service()
            reference = service.read_reference(publish_id, ordinal, historical=True)
            # 位置目录来自已发布版本的解析稿，不接受客户端指定 parse 路径。
            if reference.get('page_id'):
                units = _wiki_service().references(publish_id)
                # 兼容此前把两种 ordinal 反向命名的返回；对外统一目录索引与解析索引。
                units = [dict(unit, ordinal=i, source_ordinal=unit['ordinal'] if unit['ordinal'] != i else unit.get('source_ordinal', unit['ordinal']))
                         for i, unit in enumerate(units)]
                unit = units[ordinal]
                revision = service.registered_revision(unit['document_id'], unit['revision_id'])
                if hashlib.sha256(Path(revision['original']).read_bytes()).hexdigest() != revision['sha256']:
                    raise TaskError('storage_failed')
                parsed = json.loads((service.root / 'parses' / (unit['parse_id'] + '.json')).read_text(encoding='utf-8'))
                reference['text'] = parsed['units'][unit['source_ordinal']]['text']
                reference['location'] = unit['location']
                reference['ordinal'] = ordinal
                reference['source_ordinal'] = unit['source_ordinal']
                reference['sources'] = units
            else:
                parsed = json.loads((service.root / 'parses' / (reference['parse_id'] + '.json')).read_text(encoding='utf-8'))
                units = parsed['units']
            reference['units'] = [{**{k: unit[k] for k in ('document_id', 'revision_id', 'parse_id', 'location', 'filename', 'source_ordinal', 'source_status') if k in unit},
                                   'ordinal': i} for i, unit in enumerate(units)]
            reference['unit_count'] = len(units)
            reference.pop('original', None)
            return _reply(status='success', reference=reference)
        except (ValueError, OSError) as exc:
            return _error(exc)


class PersonalBindingHandler:
    @staticmethod
    def _auth():
        _require_auth()
        if not _desktop_token_matches():
            raise web.forbidden()

    def GET(self):
        self._auth()
        from config import conf
        return _reply(status='success', binding=conf().get('personal_feishu_binding') or {})

    def POST(self):
        self._auth()
        try:
            body = _body()
            binding = body.get('binding')
            keys = {'channel_instance_id', 'app_id', 'tenant_id', 'sender_open_id'}
            if not isinstance(binding, dict) or set(binding) != keys or any(
                not isinstance(v, str) or not v.strip() or len(v) > 256 for v in binding.values()
            ):
                raise TaskError('invalid_request')
            from channel.web.api.integrations import save_config_values
            save_config_values({'personal_feishu_binding': {k: v.strip() for k, v in binding.items()}})
            return _reply(status='success', binding=binding)
        except (ValueError, OSError) as exc:
            return _error(exc)


def _pairing_channel(instance_id):
    from channel.web.core._common import _live_channel_manager
    manager = _live_channel_manager()
    channel = manager.get_channel(instance_id) if manager and isinstance(instance_id, str) else None
    if (channel is None or channel.channel_type != 'feishu' or
            not channel.cfg('feishu_app_id') or not channel.cfg('feishu_app_secret')):
        raise TaskError('channel_unavailable')
    if channel.cfg('feishu_event_mode', 'websocket') != 'websocket':
        raise TaskError('websocket_required')
    return manager, channel


class PersonalFeishuPairingHandler:
    @staticmethod
    def _view():
        from channel.feishu.pairing import pairing
        from config import conf
        view = pairing.snapshot()
        if view['state'] in {'waiting', 'pending'}:
            from channel.web.core._common import _live_channel_manager
            manager = _live_channel_manager()
            channel = manager.get_channel(view['channel_instance_id']) if manager else None
            error = getattr(channel, '_startup_error', None)
            if isinstance(error, str) and '本人飞书绑定' not in error:
                view['connection_error'] = error
        return _reply(status='success', pairing=view, binding=conf().get('personal_feishu_binding') or {})

    def GET(self):
        PersonalBindingHandler._auth()
        return self._view()

    def POST(self):
        PersonalBindingHandler._auth()
        try:
            from common.personal_tasks import personal_enabled
            if not personal_enabled():
                raise TaskError('forbidden')
            from channel.feishu.pairing import pairing, configuration_revision
            body = _body()
            action = body.get('action')
            if action == 'start':
                manager, channel = _pairing_channel(body.get('channel_instance_id'))
                pairing.begin(channel.instance_id or 'feishu', channel.cfg('feishu_app_id'), configuration_revision)
                # A blocked startup returned before opening the socket. Restart
                # only that channel; the rest of the user's channels stay live.
                thread = getattr(channel, '_ws_thread', None)
                if thread is None or not thread.is_alive():
                    import threading
                    threading.Thread(target=manager.restart,
                                     args=(channel.instance_id or 'feishu',), daemon=True).start()
            elif action == 'confirm':
                current = pairing.snapshot()
                manager, channel = _pairing_channel(current.get('channel_instance_id'))
                from channel.web.api.integrations import save_config_values
                pairing.confirm(body.get('pairing_id'), channel.cfg('feishu_app_id'),
                                lambda binding: save_config_values({'personal_feishu_binding': binding},
                                                                  before_save=pairing.check_revision))
                from channel.feishu.personal import register
                register(channel)
                # A live binding-only connection can now receive owner messages.
                channel._personal_binding_required = False
                if channel._startup_error and '本人飞书绑定' in channel._startup_error:
                    channel.report_startup_success()
            elif action == 'cancel':
                pairing.cancel(body.get('pairing_id'))
            else:
                raise TaskError('invalid_request')
            return self._view()
        except (ValueError, OSError) as exc:
            return _error(exc)
