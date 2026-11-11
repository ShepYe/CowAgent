"""Non-secret task revision identifiers and scoped runtime configuration.

Only the identifier is persisted on tasks. Historical credentials are not
archived: after a change/restart an unavailable revision fails closed.
"""
import copy
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

from config import Config, conf, config_scope, config_lock

_pinned_config = ContextVar("personal_pinned_config", default=None)


class ConfigUnavailable(ValueError):
    code = "config_unavailable"

    def __init__(self):
        super().__init__("原配置修订已不可用，请取消原任务并重新提交")


def _revision(snapshot):
    # Includes credentials so rotating only a key invalidates old task bindings.
    # The digest is opaque metadata; neither credentials nor endpoints are
    # included in task records or exceptions.
    payload = json.dumps(dict(snapshot), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return "cfg1-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_config_revision() -> str:
    """Capture before receipt commit; do not hold config_lock at the call site."""
    pinned = _pinned_config.get()
    if pinned is not None:
        return pinned[0]
    with config_lock:
        return _revision(conf())


def require_config_revision(revision: str) -> None:
    """Reject queued/restarted/retried work whose credentials cannot be rebuilt."""
    if not isinstance(revision, str) or revision != capture_config_revision():
        raise ConfigUnavailable()


@contextmanager
def use_config_revision(revision: str):
    """Pin all conf() readers for a synchronous attempt (including retries).

    Workers spawned on other threads must enter this context themselves.
    The caller must use fresh clients, never a global Bridge cached client.
    """
    pinned = _pinned_config.get()
    if pinned is not None:
        if revision != pinned[0]:
            raise ConfigUnavailable()
        # 工具初始化可能补齐运行时字段；子能力仍使用接收时已核验的原始快照。
        with config_scope(Config(copy.deepcopy(pinned[1]))):
            yield
        return
    with config_lock:
        snapshot = copy.deepcopy(dict(conf()))
        if revision != _revision(snapshot):
            raise ConfigUnavailable()
    token = _pinned_config.set((revision, copy.deepcopy(snapshot)))
    try:
        with config_scope(Config(snapshot)):
            yield
    finally:
        _pinned_config.reset(token)


def provider_references(cfg, provider_id):
    """Return explicit capability reference paths, including fallback chains."""
    refs = []
    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in {"custom_providers", "model_catalog", "model_catalog_hidden"}:
                    walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif value == provider_id:
            refs.append(path)
    walk(dict(cfg), "")
    return refs


def validate_provider_bindings(cfg):
    providers = cfg.get("custom_providers") or []
    if not isinstance(providers, list):
        raise ValueError("custom_providers must be a list")
    ids = [p.get("id") for p in providers if isinstance(p, dict)]
    if len(ids) != len(providers) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("invalid custom provider ids")
    # Examine explicit references without guessing a vendor from model names.
    def walk(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in {"custom_providers", "model_catalog", "model_catalog_hidden"}:
                    walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f"{path}[{i}]")
        elif isinstance(value, str) and value.startswith("custom:") and value[7:] not in ids:
            raise ValueError(f"unknown custom provider reference: {path}")
    walk(dict(cfg))


def preserve_masked_secrets(updates, previous):
    """Merge masked form values with stored keys; explicit empty clears a key."""
    result = copy.deepcopy(updates)
    for key, value in result.items():
        old = previous.get(key)
        if isinstance(value, dict):
            result[key] = preserve_masked_secrets(value, old if isinstance(old, dict) else {})
        elif key == "custom_providers" and isinstance(value, list):
            by_id = {p.get("id"): p for p in (old or []) if isinstance(p, dict)}
            result[key] = [preserve_masked_secrets(
                {**by_id.get(p.get("id"), {}), **p}, by_id.get(p.get("id"), {})) for p in value]
        elif any(part in key.lower() for part in ("key", "secret", "token", "password", "authorization", "cookie")):
            if isinstance(value, str) and "*" in value:
                if old is None:
                    raise ValueError("masked secret has no stored value")
                result[key] = old
    return result
