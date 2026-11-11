import pytest

from config import Config, conf, config_scope
from models.config_revision import capture_config_revision, use_config_revision, ConfigUnavailable


def test_nested_capability_keeps_admitted_snapshot_after_runtime_normalization():
    with config_scope(Config({'model': 'admitted', 'tools': {'vision': {'model': 'vision-admitted'}}})):
        revision = capture_config_revision()
        with use_config_revision(revision):
            conf()['runtime_capability'] = True
            conf()['tools']['vision']['model'] = 'runtime-mutation'
            with use_config_revision(revision):
                assert conf()['model'] == 'admitted'
                assert conf()['tools']['vision']['model'] == 'vision-admitted'
                assert 'runtime_capability' not in conf()
            with pytest.raises(ConfigUnavailable):
                with use_config_revision('another-task-revision'):
                    pytest.fail('不得跨任务借用配置')
        conf()['model'] = 'new-setting'
        with pytest.raises(ConfigUnavailable):
            with use_config_revision(revision):
                pytest.fail('旧排队任务不能复用新配置')
