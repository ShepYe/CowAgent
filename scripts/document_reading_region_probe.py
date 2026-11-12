"""隔离真实主 Agent 局部补读探针；只保存媒体哈希、时延及用量。"""
import argparse
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time


def run(config_path, output):
    if output.exists():
        raise ValueError('output须为尚不存在的隔离目录')
    output.mkdir(parents=True)
    workspace = output / 'runtime'
    workspace.mkdir()
    os.environ['COW_DATA_DIR'] = str(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PIL import Image, ImageDraw, ImageFont
    from config import Config, config_scope
    from common.log import logger
    from common.runtime_identity import identity_scope
    from models.config_revision import capture_config_revision, use_config_revision
    from models.openai.openai_http_client import OpenAIHTTPClient
    from bridge.agent_bridge import AgentLLMModel
    from bridge.bridge import Bridge
    from agent.protocol import Agent
    from agent.tools.read.read import Read
    from agent.tools.tool_manager import ToolManager
    image = Image.new('RGB', (1200, 900), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((80, 60), '放大码 739281', fill='black',
              font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 42))
    draw.text((700, 650), 'OTHER 111111', fill='black', font=ImageFont.load_default(size=36))
    source = workspace / 'region-source.pdf'
    image.save(source, 'PDF')
    cfg = json.loads(config_path.read_text(encoding='utf-8-sig'))
    cfg.update(agent_workspace=str(workspace), debug=False, chat_fallback={'chain': []},
               document_reading_native_pdf_enabled=True, responses_prompt_cache_enabled=False,
               embedding_enabled=False, knowledge_semantic_enabled=False)
    report = dict(requests=[], tools=[], region=[0., 0., .55, .4], expected='739281')
    original = OpenAIHTTPClient.responses
    original_sync = ToolManager.sync_mcp_into_agent

    def traced(client, **kwargs):
        row = dict(media=[], elapsed_s=None)
        for item in kwargs.get('input', []):
            parts = [part for key in ('content', 'output') for part in (item[key] if isinstance(item.get(key), list) else [])]
            for part in parts:
                if part.get('type') in ('input_file', 'input_image'):
                    value = part.get('file_data') or part.get('image_url')
                    raw = base64.b64decode(value.split(',', 1)[1])
                    row['media'].append(dict(type=part['type'], sha256=sha256(raw).hexdigest(), bytes=len(raw)))
        report['requests'].append(row)
        start = time.monotonic()
        try:
            for event in original(client, **kwargs):
                response = event.get('response') or {}
                if response.get('usage'):
                    row['usage'] = response['usage']
                if event.get('type') in ('response.completed', 'response.failed', 'response.incomplete'):
                    row['terminal'] = event['type']
                yield event
        finally:
            row['elapsed_s'] = round(time.monotonic() - start, 3)

    def event(envelope):
        if envelope.get('type') == 'tool_execution_end':
            data = envelope['data']
            report['tools'].append(dict(name=data.get('tool_name'), status=data.get('status')))

    logger.setLevel('WARNING')
    OpenAIHTTPClient.responses = traced
    ToolManager.sync_mcp_into_agent = lambda *a: None
    start = time.monotonic()
    try:
        with config_scope(Config(cfg)):
            revision = capture_config_revision()
            with use_config_revision(revision), identity_scope(user_id='local-owner', session_id='region-probe'):
                model = AgentLLMModel(Bridge(), bot_type=cfg['bot_type'])
                read = Read(dict(cwd=str(workspace), document_reading_native_pdf_enabled=True))
                agent = Agent('', model=model, tools=[read], workspace_dir=str(workspace), max_steps=4,
                              enable_skills=False, skip_context_files=True, output_mode='logger')
                agent.extra_system_suffix = '必须通过read局部媒体读取数字；只回答看到的放大码及源位置。'
                answer = agent.run_stream('调用read读取region-source.pdf，pages="1", region=[0,0,0.55,0.4]。'
                                          '回答放大码数字，列出physical_page与region。', on_event=event)
                (output / 'answer.txt').write_text(answer, encoding='utf-8')
                report['correct'] = '739281' in answer and any(t['name'] == 'read' and t['status'] == 'success' for t in report['tools'])
    finally:
        OpenAIHTTPClient.responses = original
        ToolManager.sync_mcp_into_agent = original_sync
        report['elapsed_s'] = round(time.monotonic() - start, 3)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.config, args.output.resolve())
    print(json.dumps(dict(correct=result['correct'], requests=len(result['requests']), elapsed_s=result['elapsed_s'])))
