"""逐格式放行：新配置启用已验收PDF，旧配置缺键维持原路由。"""
import json
from pathlib import Path

from config import Config, config_scope
from agent.knowledge.reading_flow import enabled, office_enabled
from models.config_revision import capture_config_revision, require_config_revision, ConfigUnavailable
import pytest


def test_new_template_enables_pdf_without_requiring_office():
    template = json.loads((Path(__file__).resolve().parents[1] / 'config-template.json').read_text(encoding='utf-8'))
    with config_scope(Config(template)):
        assert enabled({})
        assert not office_enabled({}, 'sample.docx')
        assert not office_enabled({}, 'sample.pptx')
        assert template['responses_prompt_cache_enabled'] is False
        assert not enabled(dict(document_reading_native_pdf_enabled=False))


def test_legacy_config_and_inflight_revision_do_not_silently_enable_new_route():
    with config_scope(Config({})):
        assert not enabled({})
        legacy = capture_config_revision()
    with config_scope(Config(dict(document_reading_native_pdf_enabled=True))):
        assert enabled({})
        assert capture_config_revision() != legacy
        with pytest.raises(ConfigUnavailable):
            require_config_revision(legacy)


def test_office_flags_are_independent_from_pdf():
    with config_scope(Config({})):
        config = dict(document_reading_native_pdf_enabled=False, document_reading_native_docx_enabled=True)
        assert not enabled(config)
        assert office_enabled(config, 'sample.docx')
        assert not office_enabled(config, 'sample.pptx')
