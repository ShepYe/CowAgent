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
    }.get(code, '请求无法完成'), task=task)


def _body():
    body = json.loads(web.data() or b'{}')
    if not isinstance(body, dict) or 'owner' in body or 'owner_id' in body:
        raise TaskError('invalid_request')
    return body


def _source_service():
    from agent.knowledge.personal_assistant import SourceService
    return SourceService(_get_workspace_root(), owner_id=OWNER)


def _decorate(task):
    refs = []
    for ref in task.get('result_refs', []):
        ref = dict(ref)
        if ref.get('document_id'):
            try:
                service = _source_service()
                ref['source_state'] = service.get(ref['document_id'])
                current = service.current_publication(ref['document_id'], ref.get('revision_id'))
                ref['current_searchable'] = bool(current and current.get('publish_id') == ref.get('publish_id'))
            except (ValueError, OSError):
                ref['source_state'] = None
                ref['current_searchable'] = False
        refs.append(ref)
    return {**task, 'result_refs': refs}


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
            elif action == 'update_input':
                task = store.update_input(OWNER, task_id, attempt, body.get('input'))
            elif action in {'withdraw', 'correct', 'change_purpose', 'republish'}:
                task = store.get(OWNER, task_id)
                document_id = body.get('document_id')
                if task['attempt'] != attempt:
                    raise TaskError('version_conflict')
                source_ref = next((r for r in task['result_refs'] if r.get('document_id') == document_id), None)
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
            parsed = json.loads((service.root / 'parses' / (reference['parse_id'] + '.json')).read_text(encoding='utf-8'))
            reference['units'] = [{'ordinal': i, 'location': unit['location']} for i, unit in enumerate(parsed['units'])]
            reference['unit_count'] = len(parsed['units'])
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
