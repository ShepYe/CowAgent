"""个人助手持久任务的非敏感配置修订接口。"""
from models.config_revision import (
    ConfigUnavailable,
    capture_config_revision,
    require_config_revision,
    use_config_revision,
)

__all__ = ["ConfigUnavailable", "capture_config_revision",
           "require_config_revision", "use_config_revision"]
