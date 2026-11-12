"""F-01/F-07 图文验收；默认只读，按依赖由主代理分阶段调用。

  .venv/Scripts/python.exe scripts/knowledge_figures_acceptance.py baseline
  .venv/Scripts/python.exe scripts/knowledge_figures_acceptance.py prepare
  .venv/Scripts/python.exe scripts/knowledge_figures_acceptance.py --help

F07由主代理按顺序执行（E=真实联合验收JSON，A=私人备份，R=全新恢复根）：
  backup --require-evidence E --archive A
  restore --require-evidence E --archive A --restore-root R
  migrate --require-evidence E
  config-check                 # 主代理受控开启布尔后，只核对/记录，不写配置
  capability --require-evidence E
  reorganize --require-evidence E
  status / verify
图文发布后再次backup/restore到另一个新根，再qa --require-evidence E --restore-root R。
每个子命令均使用上面的Python入口；停机/重启及native开关按主代理分工处理。

baseline 不导入产品服务，只用 SQLite mode=ro 与 query_only 快照。
JSON证据只保留身份、哈希、计数、状态、时延；不写正文、路径、配置或密钥。
实际问答答案另存ignored私人local/figures-qa-answer.md供核对，不提交。
backup/restore/migrate/reorganize/qa 目前仅准备；F07阶段由主代理凭F06证据执行，无额外审批。
完整备份含私人数据和配置，--archive 须由操作者指定安全位置，不是脱敏证据。
本轮不停止/重启或迁移真实库；F07由主代理负责生命周期，migrate执行产品升级。
脚本不修改配置，不新增供应商；config-check只核对主代理实施的布尔开关变化。
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
LOCAL = REPO / 'local'
TERMINAL = {'succeeded', 'failed', 'cancelled'}
CHECKS = ('pdf_docx_pptx', 'publication_recovery', 'media_transport',
          'desktop_web', 'v3_compatibility', 'reorganize_figure_targets')
SELECTION = (
    '沿用4主题page_id、summary、分类及manual锁定、关系、depends_on和历史；不先删旧页。',
    '主Agent按正文语义选关键原图：参考平面、端口方向、校准/去嵌关系；不凑张数。',
    '优先裁图，保留端口编号、箭头、坐标、图例、公式及必要文字；裁剪不可靠才整页兜底。',
    'source绑定已成功视觉交付且有具体观察的同原件/修订/单页；扩大区域或整页先补读。',
    '新图仅接受ReadingService completed段且有visual_interpretation和实际delivery_receipt；legacy视觉缓存不算。',
    'VNA关键页必须主Agent native read实际交付并提交阅读证据；可保留旧sources并追加read alias图源。',
    '原生read固定全PDF物理页目标，按系统remaining完成多批并提交已核验reading_coverage；复用正文，不缩小目标、不改全局步数，预算失败沿原配置重试。',
    '模型仅提交figure_key/caption/source/可选region；不提供本机路径、外链或图片哈希。',
    '每主题显式figures（无需图为[]），发布前固定目标；pending/未读分开，不把文字发布算完成。',
    '图注忠实且可检索，PNG字节、当前发布和依赖在最终完成屏障重验；历史不可改写。',
    '问答须先检索正文和图注，再read受控逻辑URI；真实媒体进入主模型才算看图。',
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8'))


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def text_mark(value):
    return {'sha256': canonical(value), 'chars': len(value or '')}


def require(ok, code):
    if not ok:
        raise ValueError(code)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path).resolve()
    require(path.parent == LOCAL.resolve() and path.name.startswith('figures-')
            and path.suffix == '.json', 'evidence_path_outside_write_set')
    ignored = subprocess.run(['git', 'check-ignore', '--quiet', str(path)], cwd=REPO,
                             capture_output=True)
    require(ignored.returncode == 0, 'evidence_must_be_ignored')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def workspace(root):
    cfg = load(root / 'config.json')
    return Path(cfg.get('agent_workspace') or root / 'workspace').expanduser().resolve()


@contextmanager
def snapshot(path):
    require(path.is_file(), 'database_missing')
    source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    target = sqlite3.connect(':memory:')
    target.row_factory = sqlite3.Row
    try:
        source.execute('PRAGMA query_only=ON')
        source.backup(target)
        target.execute('PRAGMA query_only=ON')
        yield target
    finally:
        target.close()
        source.close()


def rows(db, table):
    # 表名只来自代码内固定调用，不接受用户SQL。
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [dict(r) for r in db.execute('SELECT * FROM ' + table)] if table in tables else []


def contained(path, base):
    return Path(path).resolve().is_relative_to(Path(base).resolve())


def db_baseline(root, *, relocated=False):
    w = workspace(root)
    if relocated:
        require(contained(w, root), 'restored_workspace_escapes_new_root')
    with snapshot(w / '.personal_sources/sources.sqlite3') as db:
        source_version = db.execute('PRAGMA user_version').fetchone()[0]
        require(source_version in (1, 2, 3), 'unknown_source_schema')
        documents = rows(db, 'documents')
        revisions = []
        for row in rows(db, 'revisions'):
            record = json.loads(row['record'])
            original = Path(record['original'])
            require(contained(original, w), 'original_outside_workspace')
            actual = file_hash(original)
            require(actual == record['sha256'] == row['hash'], 'original_hash_mismatch')
            revisions.append(dict(document_id=row['document'], revision_id=row['id'],
                                  sha256=actual, bytes=original.stat().st_size))
        categories = {r['page']: r for r in rows(db, 'wiki_categories')}
        publications = rows(db, 'wiki_publications')
        by_id = {r['id']: r for r in publications}
        history = []
        for row in publications:
            path = w / row['path']
            require(contained(path, w), 'publication_outside_workspace')
            actual = file_hash(path)
            require(actual == row['hash'], 'publication_hash_mismatch')
            history.append(dict(publish_id=row['id'], page_id=row['page'],
                                record_sha256=canonical(json.loads(row['record'])),
                                body_sha256=actual))
        pages = []
        for head in rows(db, 'wiki_pages'):
            row = by_id[head['current']]
            record = json.loads(row['record'])
            cat = categories.get(head['id'], {})
            pages.append(dict(page_id=head['id'], publish_id=row['id'],
                page_version=record['page_version'], status=head['status'],
                summary=text_mark(record.get('summary', '')),
                title=text_mark(record['title']), category=text_mark(cat.get('category', record['category'])),
                category_mode=cat.get('mode', 'auto'), category_revision=cat.get('revision', 0),
                related_page_ids=sorted(record.get('related_page_ids', [])),
                depends_on=record.get('depends_on', []),
                figure_manifest_sha256=canonical(record.get('figure_manifest')),
                figure_contract_present='figure_manifest' in record,
                history_count=sum(h['page_id'] == head['id'] for h in history)))
        table_marks = {}
        for table in ('wiki_sources', 'wiki_dependencies', 'wiki_category_events',
                      'figure_assets', 'figure_publications', 'reading_records'):
            values = rows(db, table)
            table_marks[table] = dict(count=len(values), sha256=canonical(sorted(values, key=canonical)))
    with snapshot(root / 'personal/tasks.sqlite3') as db:
        task_version = db.execute('PRAGMA user_version').fetchone()[0]
        require(task_version in (1, 2, 3), 'unknown_task_schema')
        tasks = [json.loads(r['record']) for r in rows(db, 'tasks')]
        task_table_marks = {}
        for table in ('figure_publications', 'reading_publications'):
            values = rows(db, table)
            task_table_marks[table] = dict(count=len(values), sha256=canonical(sorted(values, key=canonical)))
        active = [dict(task_id=t['task_id'], status=t['status'], stage=t.get('stage'))
                  for t in tasks if t['status'] not in TERMINAL]
    run_path = w / 'memory/long-term/index.db'
    running = []
    if run_path.is_file():
        with snapshot(run_path) as db:
            running = [dict(run_id_sha256=canonical(r['run_id']),
                            task_bound=bool(r['task_id']), started_at=r['started_at'])
                       for r in rows(db, 'runs') if r['status'] == 'running']
    return dict(schema_version=1, captured_at=datetime.now(timezone.utc).isoformat(),
                source_schema=source_version, task_schema=task_version,
                config_sha256=file_hash(root / 'config.json'),
                config_without_native_sha256=canonical({k: v for k, v in load(root / 'config.json').items()
                    if k != 'document_reading_native_pdf_enabled'}),
                native_pdf_enabled=load(root / 'config.json').get('document_reading_native_pdf_enabled') is True,
                documents=documents,
                revisions=revisions, unique_originals=len({r['sha256'] for r in revisions}),
                pages=sorted(pages, key=lambda p: p['page_id']),
                history=sorted(history, key=lambda h: h['publish_id']), table_marks=table_marks,
                task_table_marks=task_table_marks,
                task_status_counts=dict(Counter(t['status'] for t in tasks)), active_tasks=active,
                running_agent_records=running,
                conflict_free=not active and not running,
                privacy='摘要/分类/标题/正文只留哈希；不保存配置/密钥/路径/原始响应')


def http(args, path, body=None):
    parsed = urlparse(args.base_url)
    require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
            and not parsed.username and parsed.path in ('', '/'), 'loopback_backend_required')
    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('COW_DESKTOP_TOKEN')
    if token:
        headers['X-Cow-Desktop-Token'] = token
    request = Request(args.base_url.rstrip('/') + path, headers=headers,
                      data=json.dumps(body).encode('utf-8') if body is not None else None)
    started = time.perf_counter()
    with urlopen(request, timeout=20) as response:
        # 不跟随跳转到远端，也不持久化返回正文。
        require(urlparse(response.url).hostname == parsed.hostname, 'backend_redirect')
        content = response.read()
        return content, dict(http_status=response.status, bytes=len(content),
                             elapsed_ms=round((time.perf_counter()-started)*1000, 3),
                             sha256=sha(content))


def runtime_probe(args):
    report = {'knowledge_api': '未调用：当前GET可能实例化SourceService并写库',
              'desktop_render': '未测量', 'catalog_graph_switch': '未测量'}
    for path in ('/api/health', '/'):
        try:
            _, report[path] = http(args, path)
        except Exception as exc:
            report[path] = {'error_type': type(exc).__name__}
    if os.name == 'nt':
        command = ("$p=Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -ge 9800 -and $_.LocalPort -le 9900};"
                   "@($p | Select-Object LocalPort,OwningProcess)|ConvertTo-Json -Compress")
        result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                                capture_output=True, text=True, timeout=15)
        report['listeners'] = json.loads(result.stdout or '[]')
    return report


def same(before, after):
    for key in ('documents', 'revisions', 'pages', 'history', 'table_marks', 'task_table_marks'):
        require(before[key] == after[key], 'baseline_changed_' + key)


def source_fingerprint():
    names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others',
        '--exclude-standard', '--', 'agent', 'common', 'channel', 'cli', 'models', 'skills'], cwd=REPO)
    return canonical([(name, file_hash(REPO / name) if (REPO / name).is_file() else None)
                      for name in sorted(set(names.decode('utf-8').strip('\0').split('\0'))) if name])


def require_evidence(args):
    gate = load(args.require_evidence) if args.require_evidence else {}
    require(all(gate.get(k) is True for k in CHECKS), 'F06_joint_acceptance_required')
    require(gate.get('commit') == subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(), 'joint_commit_mismatch')
    # 包括尚未提交的新产品文件，不仅比较git diff。
    require(gate.get('source_sha256') == source_fingerprint(),
        'joint_worktree_mismatch')
    current = db_baseline(args.root)
    require(not current['active_tasks'], 'active_personal_task_conflict')
    if current['running_agent_records']:
        conflict = gate.get('runtime_conflict_check') or {}
        require(conflict.get('running_records_sha256') == canonical(current['running_agent_records'])
                and conflict.get('no_live_conflict') is True, 'runtime_conflict_evidence_required')
    return current


def prepare(args):
    baseline = load(args.baseline)
    require(len(baseline['pages']) == 4 and baseline['unique_originals'] == 1, 'unexpected_real_scope')
    save(LOCAL / 'figures-execution-plan.json', dict(
        selection_requirements=SELECTION, joint_report_required_keys=CHECKS,
        api_contract=dict(action='reorganize',
            figure_requirement=dict(schema_version=1, page_ids=sorted(p['page_id'] for p in baseline['pages'])),
            semantics='可选新字段，省略保持旧契约；显式字段参与幂等，入场校验主题属于当前document/revision；'
                      'payload保留同一对象，任务完成器固定全部page_ids且每块显式figures'),
        joint_report_binding='commit + source_sha256（含未跟踪产品文件；本模块source_fingerprint()）',
        current_source_sha256=source_fingerprint(),
        current_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        stages=['baseline', 'prepare', 'backup', 'restore', 'migrate', 'config-check', 'capability', 'reorganize', 'status', 'verify', 'qa'],
        prerequisites=['F06联合能力/失败恢复/三格式验收通过并提供记录',
                       '主代理按F07依赖调用具体子命令，无额外用户审批；执行时核对运行冲突证据',
                       '完整备份要求实例停止：F07由主代理自主停机/重启，不绕过保护',
                       '恢复到全新根，原件路径必须位于新根；恢复图文读取不得依赖原根',
                       '完整备份及异根恢复核对后，预迁移来源/任务两库v3，再提交图文任务；不绕过ensure_schema guard',
                       '运行中后端与已验收源码一致，重整API显式登记figure_publication及固定图文目标',
                       '完整备份后主代理开启原配置native_pdf；脚本config-check核对唯一布尔差异，再capability验证主模型',
                       '真实问答使用图文发布后的新备份恢复副本和原配置主模型；不切供应商；传输不支持则失败',
                       '两端UI/重启/加载/视觉语义人工核对单列，不能用HTTP首页时延代替'],
        qa_questions=['图中端口1/2的编号和箭头方向如何对应？给出所读图片和原页。',
                      '参考平面位于图中何处，校准与去嵌后如何变化？核对图中标号，不凭图注猜测。'],
        evidence_policy='只写local/figures-*.json脱敏证据；完整备份/恢复根另行指定安全位置',
        runtime_conflict_check=dict(running_records_sha256=canonical(baseline['running_agent_records']),
                                    no_live_conflict='须由运行检查证据确定，非人工审批'),
        not_executed=['备份', '恢复', '真实任务', '媒体问答', '迁移', '重启']))


def backup(args):
    before = require_evidence(args)
    require(args.archive and not args.archive.exists(), 'new_private_archive_required')
    os.environ.update(COW_DATA_DIR=str(args.root), COW_PERSONAL='1')
    from cli.commands.backup import create_backup_archive
    create_backup_archive(args.archive, args.root, workspace(args.root), complete=True)
    with zipfile.ZipFile(args.archive) as archive:
        require(archive.testzip() is None, 'archive_crc_failed')
        require(json.loads(archive.read('manifest.json'))['version'] == 3, 'complete_backup_v3_required')
    same(before, db_baseline(args.root))
    save(LOCAL / 'figures-backup.json', dict(archive_sha256=file_hash(args.archive),
         bytes=args.archive.stat().st_size, complete=True, baseline=before))


def restore(args):
    require_evidence(args)
    evidence = load(LOCAL / 'figures-backup.json')
    require(args.archive and file_hash(args.archive) == evidence['archive_sha256'], 'archive_mismatch')
    require(args.restore_root and not args.restore_root.exists(), 'fresh_restore_root_required')
    require(not contained(args.restore_root, args.root) and not contained(args.root, args.restore_root),
            'restore_root_overlap')
    os.environ.update(COW_DATA_DIR=str(args.restore_root), COW_PERSONAL='1')
    from cli.commands.backup import restore_backup_archive
    restore_backup_archive(args.archive, args.restore_root, args.restore_root / 'workspace')
    restored = db_baseline(args.restore_root, relocated=True)
    same(evidence['baseline'], restored)
    save(LOCAL / 'figures-restore.json', dict(archive_sha256=evidence['archive_sha256'],
         restore_root_sha256=canonical(str(args.restore_root)), identities_equal=True,
         path_independent_originals=True, baseline=restored,
         note='已核对相对原件/正文/历史；媒体read跨根核验由qa另行完成'))


def migrate(args):
    before = require_evidence(args)
    proof = load(LOCAL / 'figures-restore.json')
    require(proof['identities_equal'], 'restore_verification_required')
    same(load(LOCAL / 'figures-backup.json')['baseline'], before)
    os.environ.update(COW_DATA_DIR=str(args.root), COW_PERSONAL='1')
    from cli.commands.backup import _require_personal_stopped
    _require_personal_stopped(args.root)
    from agent.knowledge.reading import ReadingService
    from agent.knowledge.figures import ensure_schema
    from common.personal_tasks import TaskStore
    from common.figure_recovery import prepare_startup_recovery
    # 在来源事务外建立产品恢复点并升级两库；ensure_schema随后复用本进程恢复点。
    prepare_startup_recovery(args.root, workspace(args.root))
    # 产品迁移负责恢复点及guard；不替换、不patch，不手写来源schema。
    store = TaskStore(args.root / 'personal')
    reading = ReadingService(workspace(args.root), task_store=store)
    with reading.source.transaction() as db:
        ensure_schema(db, reading.source)
    with store._transaction() as db:
        require(db.execute('PRAGMA user_version').fetchone()[0] in (1, 2, 3), 'unknown_task_schema')
        columns = {r[1] for r in db.execute('PRAGMA table_info(figure_publications)')}
        require({'task_id', 'record'} <= columns, 'task_figure_schema_missing')
        db.execute('PRAGMA user_version=3')
    after = db_baseline(args.root)
    same(before, after)
    require(after['source_schema'] == after['task_schema'] == 3, 'both_databases_v3_required')
    save(LOCAL / 'figures-migration.json', dict(source_schema=3, task_schema=3,
         preserved=True, baseline=after))


def config_check(args):
    before = load(LOCAL / 'figures-backup.json')['baseline']
    current = db_baseline(args.root)
    require(current['native_pdf_enabled'], 'native_pdf_enabled_required')
    require(before['config_without_native_sha256'] == current['config_without_native_sha256'],
            'unexpected_config_change')
    save(LOCAL / 'figures-config-check.json', dict(before_sha256=before['config_sha256'],
        after_sha256=current['config_sha256'], only_semantic_difference='document_reading_native_pdf_enabled',
        before_enabled=before['native_pdf_enabled'], after_enabled=True,
        config_without_native_sha256=current['config_without_native_sha256']))


def capability(args):
    current = require_evidence(args)
    config_check(args)
    require(current['source_schema'] == current['task_schema'] == 3, 'premigrate_both_databases_required')
    os.environ.update(COW_DATA_DIR=str(args.root), COW_PERSONAL='1')
    from config import Config, config_scope
    from common.runtime_identity import identity_scope
    from models.config_revision import capture_config_revision, use_config_revision
    from bridge.agent_bridge import AgentLLMModel
    from bridge.bridge import Bridge
    from agent.knowledge.reading_capabilities import ensure_pdf_capability
    cfg = load(args.root / 'config.json')
    with config_scope(Config(cfg)), identity_scope(user_id='local-owner', session_id='figures-capability'):
        revision = capture_config_revision()
        with use_config_revision(revision):
            model = AgentLLMModel(Bridge(), bot_type=cfg['bot_type'])
            started = time.perf_counter()
            passed = ensure_pdf_capability(workspace(args.root), model, revision)
            save(LOCAL / 'figures-capability.json', dict(passed=passed, config_revision=revision,
                config_sha256=current['config_sha256'], elapsed_s=round(time.perf_counter()-started, 3)))
            require(passed, 'real_primary_model_pdf_capability_failed')


def reorganize(args):
    current = require_evidence(args)
    restoration = load(LOCAL / 'figures-restore.json')
    require(restoration['identities_equal'], 'restore_verification_required')
    require(current['source_schema'] == current['task_schema'] == 3, 'premigrate_both_databases_required')
    require(load(LOCAL / 'figures-migration.json')['preserved'], 'premigration_evidence_required')
    require(current['native_pdf_enabled'], 'native_pdf_enabled_required')
    config_check(args)
    media = load(LOCAL / 'figures-capability.json')
    require(media['passed'] and media['config_sha256'] == current['config_sha256'], 'real_model_capability_required')
    same(load(args.baseline), current)
    # 沿用已验证入口形状；身份/attempt/state均从实时只读记录取得。
    previous = load(REPO / 'local/summary-reorganize-request.json')
    with snapshot(args.root / 'personal/tasks.sqlite3') as db:
        parent = next(json.loads(r['record']) for r in rows(db, 'tasks')
                      if r['id'] == previous['parent_task_id'])
    doc = next(d for d in current['documents'] if d['id'] == previous['body']['document_id'])
    require(doc['purpose'] == 'long_term' and doc['validity'] == 'active', 'source_not_current')
    checkpoint = LOCAL / 'figures-admission.json'
    # 发送前落幂等身份，网络超时后仍重用，不生成第二任务。
    if checkpoint.exists():
        admission = load(checkpoint)
    else:
        admission = dict(parent_task_id=parent['task_id'], body=dict(action='reorganize',
            attempt=parent['attempt'], document_id=doc['id'], expected_state=doc['state'],
            figure_requirement=dict(schema_version=1, page_ids=sorted(p['page_id'] for p in current['pages'])),
            request_id='figures-' + canonical(current['pages'])[:24]))
        save(checkpoint, admission)
    require(admission['body'].get('figure_requirement') == dict(schema_version=1,
        page_ids=sorted(p['page_id'] for p in current['pages'])), 'fixed_four_theme_targets_required')
    raw, timing = http(args, '/api/personal/tasks/' + admission['parent_task_id'], admission['body'])
    result = json.loads(raw)
    require(result.get('status') == 'success', 'reorganize_admission_failed')
    task = result['task']
    save(LOCAL / 'figures-task.json', dict(task_id=task['task_id'], attempt=task['attempt'],
         config_revision=task['config_revision'], status=task['status'], timing=timing))


def status(args):
    # 直接读任务库；不调用可能初始化TaskStore的GET。
    task_id = load(LOCAL / 'figures-task.json')['task_id']
    with snapshot(args.root / 'personal/tasks.sqlite3') as db:
        task = next(json.loads(r['record']) for r in rows(db, 'tasks') if r['id'] == task_id)
    report = {k: task.get(k) for k in ('task_id', 'attempt', 'status', 'stage', 'outcome', 'config_revision')}
    report.update(error_sha256=canonical(task.get('error')), result_refs_sha256=canonical(task.get('result_refs')),
                  figure_publication_present='figure_publication' in task)
    save(LOCAL / 'figures-status.json', report)
    return task


def verify(args):
    old, new = load(args.baseline), db_baseline(args.root)
    require(old['revisions'] == new['revisions'], 'original_or_revision_changed')
    require(old['documents'] == new['documents'], 'document_state_changed')
    require({p['page_id'] for p in old['pages']} == {p['page_id'] for p in new['pages']}, 'theme_identity_changed')
    require({canonical(h) for h in old['history']} <= {canonical(h) for h in new['history']}, 'history_changed')
    for before in old['pages']:
        after = next(p for p in new['pages'] if p['page_id'] == before['page_id'])
        require(before['page_version'] != after['page_version'], 'theme_not_reorganized')
        for key in ('summary', 'category', 'category_mode', 'related_page_ids', 'depends_on'):
            require(before[key] == after[key], 'preservation_mismatch_' + key)
        require(after['status'] == 'active' and after['figure_contract_present'], 'theme_not_ready')
    task = status(args)
    require(task['status'] == 'succeeded' and 'figure_publication' in task, 'figure_task_incomplete')
    figures = validate_current_figures(args.root)
    # 最终PNG和来源有效性还须由产品完成屏障和qa实时读取证明，不以此报告整体成功。
    save(LOCAL / 'figures-preservation.json', dict(preserved=True, baseline=new, figures=figures,
         overall_acceptance='待真实媒体问答、语义、两端UI、重启及加载验收'))


def validate_current_figures(root):
    """复用纯验证器和只读快照；不实例化SourceService/ReadingService。"""
    from types import SimpleNamespace
    from agent.knowledge.figures import validate_manifest
    w = workspace(root)
    facade = SimpleNamespace(workspace=w, root=w / '.personal_sources')
    result = []
    with snapshot(w / '.personal_sources/sources.sqlite3') as db:
        heads = {r['current'] for r in rows(db, 'wiki_pages')}
        for row in rows(db, 'wiki_publications'):
            record = json.loads(row['record'])
            if 'figure_manifest' not in record:
                require(row['id'] not in heads, 'current_page_missing_figure_contract')
                continue
            validate_manifest(db, facade, record, check_bytes=True)
            for item in record['figure_manifest']['items']:
                require(item['state'] == 'ready' or row['id'] not in heads, 'current_figure_pending')
                result.append(dict(publish_id=row['id'], current=row['id'] in heads,
                    figure_id=item.get('figure_id'), state=item['state'], region=item['region'],
                    source_page=item.get('source_page'), caption=text_mark(item['caption']),
                    png_sha256=item.get('png_hash'), reading_id=item.get('reading_id')))
    require(any(r['current'] and r['state'] == 'ready' for r in result), 'vna_key_figures_missing')
    return result


def qa(args):
    require_evidence(args)
    require(args.restore_root and args.restore_root.exists(), 'verified_restore_root_required')
    proof = load(LOCAL / 'figures-restore.json')
    require(proof['restore_root_sha256'] == canonical(str(args.restore_root)), 'restore_root_mismatch')
    restored = db_baseline(args.restore_root, relocated=True)
    figure_checks = validate_current_figures(args.restore_root)
    require(all(p['figure_contract_present'] for p in restored['pages']), 'post_figure_backup_restore_required')
    # 仅使用恢复副本；真实主模型配置保持不变，数据目录和工作区定向副本。
    os.environ.update(COW_DATA_DIR=str(args.restore_root), COW_PERSONAL='1')
    cfg = load(args.root / 'config.json')
    w = workspace(args.restore_root)
    cfg['agent_workspace'] = str(w)
    from config import Config, config_scope
    from common.runtime_identity import identity_scope
    from models.config_revision import capture_config_revision, use_config_revision
    from models.openai.openai_http_client import OpenAIHTTPClient
    from bridge.agent_bridge import AgentLLMModel
    from bridge.bridge import Bridge
    from agent.protocol import Agent
    from agent.tools.read.read import Read
    from agent.tools.memory.memory_search import MemorySearchTool
    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    report = dict(requests=[], calls=[], answer=None, figures=figure_checks,
                  quality='待人工核对；传输计数不证明理解')
    original = OpenAIHTTPClient._request

    def trace(client, **kwargs):
        payload = kwargs.get('payload') or {}
        media = []

        def walk(value):
            if isinstance(value, dict):
                kind = value.get('type')
                if kind in ('input_image', 'image_url', 'input_file', 'file'):
                    def data_bytes(item):
                        found = []
                        if isinstance(item, str) and item.startswith('data:') and ';base64,' in item:
                            try:
                                raw = base64.b64decode(item.split(',', 1)[1], validate=True)
                                if raw:
                                    found.append(raw)
                            except ValueError:
                                pass
                        elif isinstance(item, dict):
                            for v in item.values():
                                found.extend(data_bytes(v))
                        return found
                    for raw in data_bytes(value):
                        media.append(dict(type=kind, bytes=len(raw), sha256=sha(raw)))
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
        walk(payload)
        row = dict(media=media, request_sha256=canonical(payload), usage=None)
        report['requests'].append(row)
        save(LOCAL / 'figures-qa.json', report)
        response = original(client, **kwargs)
        if not kwargs.get('stream'):
            if isinstance(response, dict):
                row['usage'] = response.get('usage')
            return response

        def stream():
            try:
                for event in response:
                    usage = (event.get('response') or {}).get('usage') or event.get('usage')
                    if usage:
                        row['usage'] = usage
                    yield event
            finally:
                save(LOCAL / 'figures-qa.json', report)
        return stream()

    OpenAIHTTPClient._request = trace
    try:
        with config_scope(Config(cfg)), identity_scope(user_id='local-owner', session_id='figures-qa'):
            revision = capture_config_revision()
            with use_config_revision(revision):
                model = AgentLLMModel(Bridge(), bot_type=cfg['bot_type'])
                read = Read(dict(cwd=str(w), workspace_root=str(w)))
                search = MemorySearchTool(MemoryManager(MemoryConfig(workspace_root=str(w))), 'local-owner')
                instruction = ('先memory_search检索端口方向与参考平面，随后read知识正文和其中knowledge-figure引用。'
                               '不得读取原始绝对路径，不得调用其他工具或重整。回答图中端口编号与箭头方向、'
                               '参考平面位置及校准/去嵌的关系；给出图片引用与原页。没有媒体明确说未看图。')
                agent = Agent(instruction, model=model, tools=[read, search], workspace_dir=str(w), max_steps=10,
                              enable_skills=False, skip_context_files=True, output_mode='logger')
                agent.extra_system_suffix = instruction
                with snapshot(w / '.personal_sources/sources.sqlite3') as db:
                    heads = {r['current'] for r in rows(db, 'wiki_pages')}
                    paths = [r['path'] for r in rows(db, 'wiki_publications') if r['id'] in heads]

                def event(envelope):
                    if envelope['type'] == 'tool_execution_end':
                        data = envelope['data']
                        report['calls'].append(dict(tool=data.get('tool_name'), status=data.get('status')))
                started = time.perf_counter()
                answer = agent.run_stream(instruction + '\n知识页：' + json.dumps(paths), on_event=event)
                answer_file = LOCAL / 'figures-qa-answer.md'
                require(subprocess.run(['git', 'check-ignore', '--quiet', str(answer_file)], cwd=REPO,
                    capture_output=True).returncode == 0, 'private_answer_must_be_ignored')
                answer_file.write_text(answer, encoding='utf-8')
                report.update(answer=text_mark(answer), elapsed_s=round(time.perf_counter()-started, 3),
                              config_revision=revision)
                require(any(r['media'] for r in report['requests']), 'no_real_media_delivery')
                report['transport_observed'] = True
    finally:
        OpenAIHTTPClient._request = original
        save(LOCAL / 'figures-qa.json', report)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=('baseline', 'prepare', 'backup', 'restore', 'migrate', 'config-check', 'capability', 'reorganize', 'status', 'verify', 'qa'))
    parser.add_argument('--root', type=Path, default=Path.home() / '.cow-personal')
    parser.add_argument('--baseline', type=Path, default=LOCAL / 'figures-baseline.json')
    parser.add_argument('--base-url', default='http://127.0.0.1:9876')
    parser.add_argument('--archive', type=Path, help='完整私人备份位置，含配置/原件，不是脱敏证据')
    parser.add_argument('--restore-root', type=Path, help='全新异根目录；严禁真实根或其父/子目录')
    parser.add_argument('--require-evidence', type=Path, help='F06真实联合验收JSON，主代理内部执行，无额外审批')
    args = parser.parse_args()
    args.root = args.root.resolve()
    if args.restore_root:
        args.restore_root = args.restore_root.resolve()
    try:
        if args.stage == 'baseline':
            require(not (LOCAL / 'figures-admission.json').exists(), 'admitted_baseline_is_frozen')
            before = db_baseline(args.root)
            before['runtime'] = runtime_probe(args)
            after = db_baseline(args.root)
            same(before, after)
            before['read_window_stable'] = True
            save(args.baseline, before)
        else:
            globals()[args.stage.replace('-', '_')](args)
        print(json.dumps(dict(stage=args.stage, completed=True), ensure_ascii=False))
    except Exception as exc:
        # 不打印原始异常（HTTP/模型异常可能包含密钥、路径、原文）。
        allowed = str(exc) if type(exc) is ValueError and str(exc).replace('_', '').isalnum() and str(exc).isascii() else None
        report = dict(stage=args.stage, completed=False, error_type=type(exc).__name__, code=allowed)
        save(LOCAL / 'figures-last-error.json', report)
        print(json.dumps(report, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
