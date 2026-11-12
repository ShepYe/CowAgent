"""The knowledge view's endpoints: /api/knowledge/*.

The document tree, a document's contents, the relation graph, and importing
new documents.
"""

import json
import re

import web

from channel.web.core._common import (
    _first_value,
    _get_workspace_root,
    _multipart_lists,
    _read_uploaded_file_bytes_limited,
    _request_agent_id,
    _require_auth,
    _scoped_agent_id,
)
from common.log import logger


def _catalog_tree(result, pages):
    """仅按当前目录的知识根完整路径投影，不向同名或历史文件借摘要。"""
    current = {p['path']: p for p in pages if p.get('searchable') and isinstance(p.get('path'), str)}

    def files(rows, parent):
        projected = []
        for row in rows:
            path = '/'.join([*parent, row['name']])
            page = current.get(path)
            # wiki 为托管发布目录；历史及失效版本不进入当前浏览结果。
            if parent and parent[0] == 'wiki' and page is None:
                continue
            item = {**row, 'summary': row.get('summary') or '',
                    'category': row.get('category') or '/'.join(parent)}
            if page is not None:
                item.update(title=page['title'], category=page.get('category', ''),
                            summary=page.get('summary') or '')
            projected.append(item)
        return projected

    def groups(rows, parent):
        projected = []
        for row in rows:
            parts = [*parent, row['dir']]
            item = {**row, 'files': files(row.get('files', []), parts),
                    'children': groups(row.get('children', []), parts)}
            if parts[0] != 'wiki' or item['files'] or item['children']:
                projected.append(item)
        return projected

    return {**result, 'root_files': files(result.get('root_files', []), []),
            'tree': groups(result.get('tree', []), [])}


class KnowledgeListHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            params = web.input(agent_id='')
            svc = KnowledgeService(
                _get_workspace_root(agent_id=_request_agent_id(params))
            )
            result = svc.list_tree()
            result = _catalog_tree(result, svc.catalog()['pages'])
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class _KnowledgeCatalogReadHandler:
    method = 'catalog'

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            from agent.knowledge.parser import SourceError
            params = web.input(agent_id='', offset='0', limit='50', publish_id='', path='')
            service = KnowledgeService(_get_workspace_root(agent_id=_request_agent_id(params)))
            if self.method == 'history':
                try:
                    offset, limit = int(params.offset), int(params.limit)
                except (ValueError, TypeError):
                    raise SourceError('invalid_request', '分页参数无效')
                result = service.history(offset, limit)
            elif self.method == 'version':
                result = {'version': service.version(params.publish_id, path=params.path)}
            else:
                result = service.catalog()
            return json.dumps({'status': 'success', **result}, ensure_ascii=False)
        except (ValueError, OSError) as exc:
            return json.dumps({'status': 'error', 'code': getattr(exc, 'code', 'invalid_request'),
                               'message': '知识请求无法完成'}, ensure_ascii=False)


class KnowledgeCatalogHandler(_KnowledgeCatalogReadHandler):
    pass


class KnowledgeHistoryHandler(_KnowledgeCatalogReadHandler):
    method = 'history'


class KnowledgeVersionHandler(_KnowledgeCatalogReadHandler):
    method = 'version'


class KnowledgeReadHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from pathlib import Path
            from agent.knowledge.service import KnowledgeService
            params = web.input(path='', agent_id='')
            svc = KnowledgeService(
                _get_workspace_root(agent_id=_request_agent_id(params))
            )
            result = svc.read_file(params.path)
            if str(result['path']).startswith('wiki/'):
                version = svc.version(path=result['path'])
                result.update({key: version[key] for key in ('page_id', 'publish_id', 'figure_state', 'figure_manifest') if key in version})
            # Absolute directory of the doc (posix separators), so clients can
            # resolve image srcs that are relative to the doc into /api/file
            # URLs. Additive field; read_file itself stays untouched.
            rel = str(result["path"]).replace("\\", "/")
            result["dir"] = Path(svc.knowledge_dir, *rel.split("/")).parent.as_posix()
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


def _figure_params():
    from agent.knowledge.parser import SourceError
    params = web.input(agent_id='', publish_id='', figure_id='', figure_key='', view='current', material='original')
    if any(getattr(params, key, None) is not None for key in ('path', 'owner', 'owner_id', 'reading_id', 'parse_id')):
        raise SourceError('invalid_request', '图片请求不接受文件路径或内部身份')
    if not isinstance(params.publish_id, str) or params.material not in ('original', 'reading-copy') or params.view not in ('current', 'history') or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', params.publish_id):
        raise SourceError('invalid_request', '图片视图或发布身份无效')
    return params


def _figure_error(exc):
    web.header('Content-Type', 'application/json; charset=utf-8')
    web.header('Cache-Control', 'no-store')
    code = getattr(exc, 'code', 'storage_failed')
    web.ctx.status = {'invalid_request': '400 Bad Request', 'forbidden': '403 Forbidden',
                      'version_conflict': '409 Conflict'}.get(code, '422 Unprocessable Entity')
    return json.dumps(dict(status='error', code=code, message='知识图片请求无法完成'), ensure_ascii=False)


class KnowledgeFigureHandler:
    def GET(self):
        _require_auth()
        try:
            from agent.knowledge.parser import SourceError
            params = _figure_params()
            if not isinstance(params.figure_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', params.figure_id):
                raise SourceError('invalid_request', '图片身份无效')
            workspace = _get_workspace_root(agent_id=_request_agent_id(params))
            from agent.knowledge.figures import resolve_figure
            result = resolve_figure(workspace, params.publish_id, params.figure_id, view=params.view)
            web.header('Content-Type', result['mime_type'])
            web.header('Cache-Control', 'no-store')
            web.header('X-Content-Type-Options', 'nosniff')
            return result['data']
        except KeyError:
            from agent.knowledge.parser import SourceError
            return _figure_error(SourceError('forbidden', '图片作用域无效'))
        except (ValueError, OSError) as exc:
            return _figure_error(exc)


class KnowledgeFigureOriginalHandler:
    def GET(self):
        _require_auth()
        try:
            import mimetypes
            from pathlib import Path
            from agent.knowledge.parser import SourceError
            from agent.knowledge.wiki import WikiService
            params = _figure_params()
            if not isinstance(params.figure_key, str) or not params.figure_key or len(params.figure_key) > 256:
                raise SourceError('invalid_request', '图片键无效')
            wiki = WikiService(_get_workspace_root(agent_id=_request_agent_id(params)))
            ref = wiki.read_reference(params.publish_id, historical=params.view == 'history')
            manifest = ref.get('figure_manifest', {})
            if manifest.get('schema_version') != 1:
                raise SourceError('invalid_request', '图片清单版本无效')
            item = next((i for i in manifest['items'] if i['figure_key'] == params.figure_key), None)
            if item is None:
                raise SourceError('invalid_request', '图片不存在')
            identity = ('document_id', 'revision_id', 'parse_id', 'ordinal')
            ordinal = next((i for i, source in enumerate(ref['sources'])
                            if all(source.get(k) == item['source'].get(k) for k in identity)), None)
            if ordinal is None:
                raise SourceError('invalid_request', '图片出处不存在')
            source = wiki.read_reference(params.publish_id, ordinal, historical=params.view == 'history')
            if params.material == 'reading-copy':
                import hashlib
                from agent.knowledge.reading import _read
                from agent.knowledge.reading_materials import load_pdf_materials, _relative
                with wiki.source.transaction() as db:
                    reading = _read(db, item['reading_id'])
                    material = load_pdf_materials(wiki.workspace, reading['manifest'], expected_manifest_hash=reading['manifest_hash'])
                    data = _relative(wiki.workspace, material.original_relpath).read_bytes()
                    if hashlib.sha256(data).hexdigest() != material.original_hash or not data.startswith(b'%PDF-'):
                        raise SourceError('storage_failed', '阅读副本校验失败')
                mime_type = 'application/pdf'
            else:
                data = Path(source['original']).read_bytes()
                mime_type = mimetypes.guess_type(source.get('filename', ''))[0] or 'application/octet-stream'
            web.header('Content-Type', mime_type)
            web.header('Cache-Control', 'no-store')
            web.header('X-Content-Type-Options', 'nosniff')
            return data
        except KeyError:
            from agent.knowledge.parser import SourceError
            return _figure_error(SourceError('forbidden', '图片作用域无效'))
        except (ValueError, OSError) as exc:
            return _figure_error(exc)


class KnowledgeGraphHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            params = web.input(agent_id='')
            svc = KnowledgeService(
                _get_workspace_root(agent_id=_request_agent_id(params))
            )
            return json.dumps(svc.build_graph(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge graph error: {e}")
            return json.dumps({"nodes": [], "links": []})


class KnowledgeActionHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            if not isinstance(body, dict) or not isinstance(body.get('payload', {}), dict):
                return json.dumps({'status': 'error', 'code': 400, 'message': '请求参数无效', 'payload': None}, ensure_ascii=False)
            action = body.get("action", "")
            payload = body.get("payload") or {}
            from agent.knowledge.service import KnowledgeService
            # Answer once the files are written; the reindex - a full scan,
            # possibly an embedding call, possibly a wait on the index the
            # Agent is using - runs behind the response.
            result = KnowledgeService(
                _get_workspace_root(agent_id=_scoped_agent_id(body)),
                reindex_in_background=True,
            ).dispatch(action, payload)
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge action error: {e}")
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})


class KnowledgeImportHandler:
    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.knowledge.service import KnowledgeService
            content_length = int(getattr(web.ctx, "env", {}).get("CONTENT_LENGTH") or 0)
            if content_length > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                return json.dumps({
                    "status": "error",
                    "code": 413,
                    "message": "import batch too large",
                    "payload": None,
                })
            params = _multipart_lists(KnowledgeService.MAX_IMPORT_FILES * 2 + 16)
            agent_id = _scoped_agent_id(params)
            target_category = _first_value(params, "target_category", "")
            conflict_strategy = _first_value(params, "conflict_strategy", "skip")
            uploaded = list(params.get("files") or []) + list(params.get("file") or [])
            if not uploaded:
                return json.dumps({"status": "error", "code": 400, "message": "No files uploaded", "payload": None})
            if len(uploaded) > KnowledgeService.MAX_IMPORT_FILES:
                return json.dumps({
                    "status": "error",
                    "code": 400,
                    "message": f"too many files: max {KnowledgeService.MAX_IMPORT_FILES}",
                    "payload": None,
                })

            files = []
            total_size = 0
            for file_obj in uploaded:
                if file_obj is None:
                    continue
                filename = getattr(file_obj, "filename", "") or getattr(file_obj, "name", "")
                content = _read_uploaded_file_bytes_limited(file_obj, KnowledgeService.MAX_IMPORT_FILE_SIZE)
                total_size += len(content)
                if total_size > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                    return json.dumps({
                        "status": "error",
                        "code": 413,
                        "message": "import batch too large",
                        "payload": None,
                    })
                files.append({
                    "filename": filename,
                    "content": content,
                })

            result = KnowledgeService(
                _get_workspace_root(agent_id=agent_id),
                reindex_in_background=True,
            ).dispatch("import_documents", {
                "target_category": target_category,
                "conflict_strategy": conflict_strategy,
                "files": files,
            })
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge import error: {e}", exc_info=True)
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})
