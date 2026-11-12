"""Credentials of the built-in ``cowagent`` provider.

The CowAgent model API is OpenAI-compatible (chat, images, audio, embeddings)
plus a web search endpoint, so every capability reuses the generic
OpenAI-compatible code paths with this one key and base.
"""

import os

from common import const, i18n
from config import conf


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def default_cowagent_api_base() -> str:
    """The site an unset base points at: the China site for a Chinese UI, otherwise the global one."""
    return const.COWAGENT_API_BASE_CN if i18n.get_language() == i18n.ZH else const.COWAGENT_API_BASE_GLOBAL


def resolve_cowagent_credentials():
    """Return ``(api_key, api_base)``: config first, then environment, then
    the site matching the UI language."""
    settings = conf()
    if settings.get('_cowagent_credentials_bound') is True:
        return _text(settings.get('cowagent_api_key')), _text(settings.get('cowagent_api_base'))
    api_key = _text(settings.get("cowagent_api_key")) or _text(os.environ.get("COWAGENT_API_KEY"))
    api_base = (
        _text(settings.get("cowagent_api_base"))
        or _text(os.environ.get("COWAGENT_API_BASE"))
        or default_cowagent_api_base()
    )
    return api_key, api_base.rstrip("/")


def bind_cowagent_config(snapshot):
    """将新渠道的有效连接写入任务内存快照，旧渠道的修订保持原样。"""
    def selected(value):
        if isinstance(value, dict):
            return any(selected(child) for key, child in value.items()
                       if key not in {'custom_providers', 'model_catalog', 'model_catalog_hidden'})
        if isinstance(value, list):
            return any(selected(child) for child in value)
        return isinstance(value, str) and value.strip().lower() == const.COWAGENT

    if snapshot.get('_cowagent_credentials_bound') is True or not selected(snapshot):
        return snapshot
    key = _text(snapshot.get('cowagent_api_key')) or _text(os.environ.get('COWAGENT_API_KEY'))
    base = (_text(snapshot.get('cowagent_api_base')) or _text(os.environ.get('COWAGENT_API_BASE'))
            or default_cowagent_api_base())
    snapshot.update(cowagent_api_key=key, cowagent_api_base=base.rstrip('/'),
                    _cowagent_credentials_bound=True)
    return snapshot


def bound_cowagent_environment():
    """技能子进程使用同一任务的连接，覆盖稍后变化的进程环境。"""
    if conf().get('_cowagent_credentials_bound') is not True:
        return {}
    key, base = resolve_cowagent_credentials()
    return {'COWAGENT_API_KEY': key, 'COWAGENT_API_BASE': base}
