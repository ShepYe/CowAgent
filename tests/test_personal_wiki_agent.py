"""确定性模型替身验证真实 Agent 工具回路；不评价真实模型的整理质量。"""
import json
from pathlib import Path

import pytest

from agent.protocol import Agent, LLMModel
from agent.skills.manager import SkillManager
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.wiki import WikiService
from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager
from agent.tools.knowledge_ingest import KnowledgeIngest
from agent.tools.memory.memory_search import MemorySearchTool
from agent.tools.read.read import Read
from common.personal_intake import task_prompt
from common.personal_tasks import OWNER, TaskStore, task_scope
from common.runtime_identity import identity_scope
from scripts.verify_personal_desktop import choose_response


class DeterministicToolLoop(LLMModel):
    """只替换模型传输；工具执行、Agent 调度、技能和索引均使用生产实现。"""
    def __init__(self, page_count=1):
        super().__init__('offline-tool-loop')
        self.evidence = []
        self.calls = []
        self.page_count = page_count

    def call_stream(self, request):
        messages = [{'role': 'system', 'content': request.system}]
        # Agent 的统一内容块转换成替身 HTTP 服务使用的 OpenAI 线格式。
        for message in request.messages:
            content = message.get('content')
            if isinstance(content, str):
                messages.append(message)
                continue
            texts, calls = [], []
            for block in content or []:
                if block.get('type') == 'text':
                    texts.append(block['text'])
                elif block.get('type') == 'tool_use':
                    calls.append(dict(id=block['id'], type='function', function=dict(
                        name=block['name'], arguments=json.dumps(block['input']))))
                elif block.get('type') == 'tool_result':
                    raw = block['content']
                    messages.append(dict(role='tool', tool_call_id=block['tool_use_id'],
                                         content=raw if isinstance(raw, str) else json.dumps(raw)))
            if texts or calls:
                wire = dict(role=message['role'], content='\n'.join(texts))
                if calls:
                    wire['tool_calls'] = calls
                messages.append(wire)
        delta, stats = choose_response(dict(messages=messages, tools=[t if 'function' in t else dict(type='function', function=dict(name=t['name'], parameters=t.get('input_schema', {}))) for t in request.tools]))
        # 本地模型夹具遵循新契约；共享安装探针由其负责者维护。
        for call in delta.get('tool_calls', []):
            if call['function']['name'] != 'knowledge_ingest':
                continue
            args = json.loads(call['function']['arguments'])
            if args.get('pages'):
                page = args['pages'][0]
                if self.page_count == 2:
                    import copy
                    other = copy.deepcopy(page)
                    other['title'] = '预算测量对照'
                    args['pages'].append(other)
                for page in args['pages']:
                    page['summary'] = '汇集已读取资料中的链路预算测量值，供工程比较与核验原文时使用。'
                call['function']['arguments'] = json.dumps(args, ensure_ascii=False)
        self.evidence.append(stats)
        self.calls.extend(delta.get('tool_calls', []))
        yield {'choices': [{'delta': delta, 'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}]}


@pytest.mark.parametrize('attachment_count', [1, 2])
@pytest.mark.parametrize('page_count', [1, 2])
@pytest.mark.parametrize('purpose,marker,status', [
    ('long_term', '', 'succeeded'), ('transient', 'P01_TRANSIENT', 'succeeded'),
    ('waiting', 'P01_WAITING', 'waiting_input')])
def test_real_agent_reads_skill_evidence_decides_and_organizes(tmp_path, monkeypatch, purpose, marker, status, attachment_count, page_count):
    import socket
    from common import state_dir
    from agent.tools.tool_manager import ToolManager
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('离线验收不得出网'))
    monkeypatch.setattr(state_dir, 'knowledge_dir', lambda **kw: tmp_path / 'knowledge')
    monkeypatch.setattr(state_dir, 'real_state_root', lambda: str(tmp_path))
    # 本任务只使用显式工具，不启动用户的 MCP 连接。
    monkeypatch.setattr(ToolManager, 'sync_mcp_into_agent', lambda *a: None)
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, purpose, {'text': marker + ' 分享资料'}, {'channel': 'web'}, 'offline')
    original = tmp_path / 'unhelpful-name.txt'
    original.write_text('测试测量结果：antenna budget 42 dB。\n仅作附注：设备已校准。', encoding='utf-8')
    store.attach(OWNER, task['task_id'], original)
    if attachment_count == 2:
        other = tmp_path / 'other.txt'
        other.write_text('另一个系统的 antenna budget 57 dB。', encoding='utf-8')
        store.attach(OWNER, task['task_id'], other)
    task = store.claim(OWNER, task['task_id'])
    config = dict(cwd=str(tmp_path), task_store=store)
    skills = SkillManager(custom_dir=str(tmp_path / 'skills'))
    skill = skills.get_skill('knowledge-wiki').skill
    assert skill and 'pages[].summary' in skill.content
    assert skill.source == 'builtin'
    assert Path(skill.file_path).resolve() == Path('skills/knowledge-wiki/SKILL.md').resolve()
    model = DeterministicToolLoop(page_count)
    manager = MemoryManager(MemoryConfig(workspace_root=str(tmp_path)), embedding_provider=None)
    try:
        agent = Agent('按资料实际整理知识。', model=model,
            tools=[Read(config), KnowledgeIngest(config), MemorySearchTool(manager, OWNER)],
            workspace_dir=str(tmp_path), skill_manager=skills, max_steps=10, output_mode='logger')
        with identity_scope(user_id=OWNER), task_scope(task['task_id']):
            response = agent.run_stream(marker + ' 分享资料' + task_prompt(task), skill_filter=['knowledge-wiki'])
        assert response.startswith('INGEST_OK'), response
        after = store.get(OWNER, task['task_id'])
        assert after['status'] == status
        assert Path(after['attachments'][0]['path']).read_bytes() == original.read_bytes()
        last = model.evidence[-1]
        assert last['skill_read'] and last['attachment_reads'] == attachment_count and last['tool_success']
        assert Path(last['skill_path']).resolve() == Path(skill.file_path).resolve()
        names = [c['function']['name'] for c in model.calls]
        assert names[:attachment_count + 2] == ['read'] * (attachment_count + 1) + ['knowledge_ingest']
        args = [json.loads(c['function']['arguments']) for c in model.calls if c['function']['name'] == 'knowledge_ingest']
        assert args[0]['purpose'] == purpose and 'pages' not in args[0]
        if purpose == 'long_term':
            assert len(args) == 2 and last['pages_submitted']
            assert len(args[1]['pages']) == page_count
            assert all(1 <= len(p['summary']) <= 160 and '\n' not in p['summary'] for p in args[1]['pages'])
            page = args[1]['pages'][0]
            assert page['title'] == '链路预算要点' and page['category'] == '工程/射频'
            assert page['blocks'][0]['content'] != original.read_text(encoding='utf-8')
            ref = after['result_refs'][0]
            readback = WikiService(tmp_path).read_reference(ref['publish_id'])
            assert readback['content'] == page['blocks'][0]['content']
            assert readback['sources'][0]['text'] == '测试测量结果：antenna budget 42 dB。'
            assert readback['sources'][0]['parse_id'] == page['blocks'][0]['sources'][0]['parse_id']
            with identity_scope(user_id=OWNER):
                found = agent.run_stream('P01_SEARCH budget', clear_history=True)
            assert 'SEARCH_RESULT' in found and ref['publish_id'] in found
            assert '42 dB' in found
            assert len(readback['sources']) == attachment_count
            assert len(after['result_refs']) == page_count
            for submitted_page, result_ref in zip(args[1]['pages'], after['result_refs']):
                published = WikiService(tmp_path).read_reference(result_ref['publish_id'])
                assert published['summary'] == submitted_page['summary']
            if attachment_count == 2:
                assert '57 dB' in found
        else:
            assert len(args) == 1 and not last['pages_submitted']
            assert not SourceService(tmp_path).search('budget')
            if purpose == 'waiting':
                assert after['question'] == '应替代哪份资料？'
            else:
                assert after['outcome'] == 'transient'
    finally:
        manager.close()


def test_custom_skill_keeps_normal_precedence_and_user_file(tmp_path):
    custom_dir = tmp_path / 'skills'
    builtin = SkillManager(custom_dir=str(custom_dir)).get_skill('knowledge-wiki').skill
    assert builtin.source == 'builtin' and 'pages' in builtin.content
    custom = custom_dir / 'knowledge-wiki' / 'SKILL.md'
    custom.parent.mkdir(parents=True)
    custom.write_text(builtin.content + '\n用户自定义：保留我的分类约定。\n', encoding='utf-8')
    before = custom.read_bytes()
    manager = SkillManager(custom_dir=str(custom_dir))
    selected = manager.get_skill('knowledge-wiki').skill
    assert selected.source == 'custom' and Path(selected.file_path) == custom
    assert '保留我的分类约定' in selected.content
    manager.refresh_skills(use_cache=True)
    assert manager.get_skill('knowledge-wiki').skill.source == 'custom'
    assert custom.read_bytes() == before
