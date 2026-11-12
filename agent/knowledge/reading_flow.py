"""PDF 阅读路线的可信准备入口；先固定整个任务目标，再接受模型页窗。"""
from pathlib import Path

from agent.knowledge.parser import SourceError


def enabled(config):
    # 格式端到端验收后才放行，开发隔离实例可以显式开启。
    from config import conf
    return config.get('document_reading_native_pdf_enabled',
                      conf().get('document_reading_native_pdf_enabled', False)) is True


def office_enabled(config, filename):
    from config import conf
    suffix = Path(filename).suffix.lower()
    key = {'.docx': 'document_reading_native_docx_enabled',
           '.pptx': 'document_reading_native_pptx_enabled'}.get(suffix)
    return bool(key) and config.get(key, conf().get(key, False)) is True


def office_options(config):
    from config import conf
    return dict(soffice=config.get('document_reading_soffice', conf().get('document_reading_soffice', 'soffice')),
                timeout_seconds=config.get('document_reading_conversion_timeout',
                                           conf().get('document_reading_conversion_timeout', 120)))


def prepare_task_target(store, task, workspace, config=None):
    """全部长期PDF资料的物理页来自可信原件，模型不能以少交出处缩小目标。"""
    from pypdf import PdfReader
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.reading_publication import register_target
    config = config or {}
    long_term = [a for a in task['attachments']
                 if (task.get('attachment_decisions') or {}).get(a['attachment_id'], {}).get('purpose') == 'long_term']
    if any(office_enabled(config, a['filename']) for a in long_term):
        for attachment in long_term:
            suffix = Path(attachment['filename']).suffix.lower()
            if suffix != '.pdf' and not office_enabled(config, attachment['filename']):
                raise SourceError('attachment_unsupported', '本批次包含尚未启用原生阅读的格式')
        from agent.knowledge.office_reading_flow import prepare_office_task_target
        return prepare_office_task_target(store, task, workspace, **office_options(config))
    service = SourceService(workspace, task['owner_id'])
    decisions = task.get('attachment_decisions') or {}
    if len(decisions) != len(task['attachments']):
        raise SourceError('invalid_request', '原生入库前须先登记全部附件用途')
    targets = []
    for attachment in task['attachments']:
        decision = decisions[attachment['attachment_id']]
        if decision['purpose'] != 'long_term':
            continue
        if Path(attachment['filename']).suffix.lower() != '.pdf':
            raise SourceError('attachment_unsupported', '本轮原生路线仅放行PDF资料批次')
        entry = store.attachment(task['owner_id'], task['task_id'], attachment['attachment_id'])
        receipt = service.registered_revision(decision['source']['document_id'], decision['source']['revision_id'])
        if receipt['sha256'] != entry['sha256']:
            raise SourceError('version_conflict', '原生阅读目标与资料登记不符')
        targets.extend(f"{receipt['revision_id']}:{p}" for p in range(1, len(PdfReader(entry['path']).pages) + 1))
    if not targets:
        raise SourceError('invalid_request', '没有长期PDF阅读目标')
    return register_target(store, task, targets)


def prepare_pdf_read(store, task, workspace, receipt, model, first, last, budget=None, config=None):
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.reading_materials import prepare_pdf_materials, PdfBudget
    from agent.knowledge.reading import ReadingService
    from agent.knowledge.reading_capabilities import capability_identity
    task = prepare_task_target(store, task, workspace, config)
    source = SourceService(workspace, task['owner_id'])
    with store.commit_guard(task['owner_id'], task['task_id'], task['attempt']):
        current = store.get(task['owner_id'], task['task_id'])
        anchors = dict(current.get('source_native_parses') or {})
        parsed = source.parse(receipt['revision_id'], prepare_only=True,
                              base_parse_id=anchors.get(receipt['revision_id']))
        anchors[receipt['revision_id']] = parsed['parse_id']
        store._change(task['owner_id'], task['task_id'], task['attempt'], {'running'}, source_native_parses=anchors)
    relative = '.personal_sources/originals/' + receipt['sha256']
    # 任务目标已独立固定；只准备本次页窗，局部问题不会外发整份原件。
    materials = prepare_pdf_materials(workspace, relative, document_id=receipt['document_id'],
        revision_id=receipt['revision_id'], parse_id=parsed['parse_id'],
        config_revision=task['config_revision'], target_pages=range(first, last + 1),
        budget=budget or PdfBudget())
    return task, parsed, materials, ReadingService(workspace, task_store=store), capability_identity(model, task['config_revision'])
