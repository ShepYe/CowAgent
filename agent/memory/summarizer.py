"""上下文摘要与持久候选分离；长期内容仅通过记忆台账提交。"""
import copy
import hashlib
import json
import threading
from typing import Optional, Any
from pathlib import Path
from datetime import datetime
from common.log import logger
from common.runtime_identity import identity_scope


SUMMARIZE_SYSTEM_PROMPT_ZH = """你是对话压缩助手。只返回 JSON：
{"context_summary":"供当前任务继续推理的摘要", "entries":[{"content":"一个独立事实", "disposition":"conversation_memory|sourced_knowledge|archive_only|undecided", "basis":[{"message_id":"原话ID", "block_index":null, "start":0, "end":1, "sha256":"原始整块文本的SHA-256"}], "reason":"简短原因"}]}
context_summary 可保留测试、临时任务和资料内容以维持上下文，但不是可写入日记的事实。
entries 只是候选，不能自报批准；只提出有价值的独立事实，没候选时返回 []。
依据取自提供的 evidence；block_index 为原始文本块下标（字符串正文为 null），start/end 为 Unicode 码点区间，sha256 沿用原始整块哈希。
同一句中真实偏好与测试数值分别引用片段；资料原文、复述与跨轮指代走 sourced_knowledge；测试、模拟及其过程走 archive_only；不足以判断走 undecided。
role=user 包装的工具结果、重放和压缩摘要不是真人原话。助手的猜测不能成为本人事实。不得仅因会话读过资料而删除独立普通交流。"""
SUMMARIZE_SYSTEM_PROMPT_EN = SUMMARIZE_SYSTEM_PROMPT_ZH
SUMMARIZE_USER_PROMPT_ZH = "请压缩以下带原话依据的当前上下文：\n{conversation}"
SUMMARIZE_USER_PROMPT_EN = "Compress this context with evidence:\n{conversation}"
DREAM_SYSTEM_PROMPT_ZH = """你是记忆整理助手。只基于给出的当前有效条目合并相近内容，不新增事实，不从旧日记或原始留档推测。
返回 JSON：{"merges":[{"content":"合并正文", "input_ids":["输入entry_id", "另一个entry_id"]}]}。
每次合并至少两个条目，保留所有输入身份，分组不重叠。相互独立的事实不要强行合并，不能解决的冲突留给主 Agent 判断；无合并则 merges=[]。不输出梦境日记。"""
DREAM_SYSTEM_PROMPT_EN = DREAM_SYSTEM_PROMPT_ZH


def _is_en():
    try:
        from common import i18n
        return i18n.get_language() == "en"
    except Exception:
        return False


def _summarize_system_prompt():
    return SUMMARIZE_SYSTEM_PROMPT_EN if _is_en() else SUMMARIZE_SYSTEM_PROMPT_ZH


def _summarize_user_prompt():
    return SUMMARIZE_USER_PROMPT_EN if _is_en() else SUMMARIZE_USER_PROMPT_ZH


def _is_empty_sentinel(text):
    return not text or text.strip().lower() in ("", "无", "none")


def _json_object(raw):
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _message_key(ledger, messages):
    # 依赖程序消息身份，不能因台账回填 candidate/decision 元数据重复计费。
    return _digest(dict(scope=ledger.scope, messages=[dict(message_id=m.get("message_id"),
                         content=m.get("content"), archive_only=(m.get("extras") or {}).get("archive_only"),
                         purpose=m.get("purpose"), personal_task_id=m.get("personal_task_id")) for m in messages]))


class MemoryFlushManager:
    def __init__(self, workspace_dir: Path, llm_model: Optional[Any] = None):
        self.workspace_dir = Path(workspace_dir)
        self.llm_model = llm_model
        self.memory_dir = self.workspace_dir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.last_flush_timestamp = None
        self._trim_flushed_hashes = set()
        self._trim_inflight_hashes = set()
        self._daily_flushed_hashes = set()
        self._daily_inflight_hashes = set()
        self._last_flushed_content_hash = ""
        self._flush_hashes_lock = threading.Lock()
        self._last_dream_input_hash = ""
        self._last_flush_thread = None
        self.last_receipt = None

    def _ledger(self):
        from agent.memory.ledger import MemoryLedger
        return MemoryLedger(self.workspace_dir)

    def capture(self, messages):
        """同步保存原话及身份；未知入口沿用 internal 缺省。"""
        return self._ledger().capture(messages)

    def get_today_memory_file(self, user_id=None, ensure_exists=False):
        if ensure_exists:
            return ensure_daily_memory_file(self.workspace_dir, user_id)
        base = self.memory_dir / "users" / user_id if user_id else self.memory_dir
        return base / f"{datetime.now():%Y-%m-%d}.md"

    def get_main_memory_file(self, user_id=None):
        return (self.memory_dir / "users" / user_id / "MEMORY.md" if user_id
                else self.workspace_dir / "MEMORY.md")

    def get_status(self):
        return dict(last_flush_time=self.last_flush_timestamp.isoformat() if self.last_flush_timestamp else None,
                    today_file=str(self.get_today_memory_file()), main_file=str(self.get_main_memory_file()))

    def summarize_and_record(self, messages, max_messages=0, *, with_revision=False):
        """裁剪前完成原话和候选保存。模型失败只提供上下文 fallback。"""
        ledger = self._ledger()
        ledger.capture(messages)
        revision = ledger.revision()
        snapshot = copy.deepcopy(messages)
        result = self._summarize_messages(snapshot, max_messages)
        if result is None:
            raise ValueError("摘要没有可用结果，请保留原话重试")
        entries = result.get("entries", []) if isinstance(result, dict) else []
        summary = self._clean_summary_output(result)
        if entries:
            request_id = "summary:" + _digest(dict(revision=revision, entries=entries))
            receipt = ledger.record(request_id, entries, trusted=False, expected_revision=revision)
            self.last_receipt = receipt
            if any(item.get("status") == "failed" for item in receipt.get("results", [])):
                raise ValueError("记忆候选提交失败，保留上下文以便重新核验")
            revision = receipt["revision"]
        # 无候选的摘要同样属于旧快照，不能越过更正屏障。
        if ledger.revision() != revision:
            raise ValueError("记忆已变更，丢弃迟到上下文摘要")
        retryable = isinstance(result, dict) and result.get("_retryable", False)
        return (summary, revision, retryable) if with_revision else summary

    def flush_from_messages(self, messages, user_id=None, reason="trim", max_messages=0,
                            context_summary_callback=None, daily_content_hash=None):
        if not messages:
            return False
        key = None
        claimed = False
        try:
            with identity_scope(**({"user_id": user_id} if user_id is not None else {})):
                self.capture(messages)
                key = _message_key(self._ledger(), messages)
                with self._flush_hashes_lock:
                    if key in self._trim_flushed_hashes:
                        if daily_content_hash:
                            self._daily_flushed_hashes.add(daily_content_hash)
                            self._last_flushed_content_hash = daily_content_hash
                        return True
                    if key in self._trim_inflight_hashes:
                        return False
                    self._trim_inflight_hashes.add(key)
                    claimed = True
                summary, revision, retryable = self.summarize_and_record(messages, max_messages, with_revision=True)
                # 提交期间的用途更正也不能让旧摘要注回新上下文。
                if self._ledger().revision() != revision:
                    return False
                if context_summary_callback and summary:
                    if hasattr(context_summary_callback, "_memory_expected_revision"):
                        context_summary_callback._memory_expected_revision = revision
                    context_summary_callback(summary)
                if revision != self._ledger().revision():
                    return False
                # 无模型的 fallback 不消耗之后重新提取候选的资格。
                if not retryable:
                    with self._flush_hashes_lock:
                        self._trim_flushed_hashes.add(key)
                        if daily_content_hash:
                            self._daily_flushed_hashes.add(daily_content_hash)
                            self._last_flushed_content_hash = daily_content_hash
                self.last_flush_timestamp = datetime.now()
                return True
        except Exception as exc:
            logger.warning(f"[MemoryFlush] 原话/候选未完成保存，保留上下文 (reason={reason}): {exc}")
            return False
        finally:
            with self._flush_hashes_lock:
                if key and claimed:
                    self._trim_inflight_hashes.discard(key)
                if daily_content_hash:
                    self._daily_inflight_hashes.discard(daily_content_hash)

    def write_daily_summary(self, summary, user_id=None, reason="trim"):
        """兼容旧调用方；任意摘要文本不再具有持久写入权限。"""
        return False

    @staticmethod
    def _clean_summary_output(raw):
        if isinstance(raw, dict):
            return str(raw.get("context_summary") or "").strip()
        value = _json_object(raw)
        if value is not None:
            return str(value.get("context_summary") or "").strip()
        if _is_empty_sentinel(raw):
            return ""
        text = raw.strip()
        if "[DAILY]" in text:
            text = text.split("[DAILY]", 1)[1]
        return text.split("[MEMORY]", 1)[0].replace("```", "").strip()

    def create_daily_summary(self, messages, user_id=None):
        with identity_scope(**({"user_id": user_id} if user_id is not None else {})):
            self.capture(messages)
            key = _message_key(self._ledger(), messages)
            with self._flush_hashes_lock:
                if key in self._daily_flushed_hashes or key in self._daily_inflight_hashes:
                    return False
                self._daily_inflight_hashes.add(key)
            return self.flush_from_messages(messages, user_id=user_id, reason="daily_summary", daily_content_hash=key)

    def deep_dream(self, user_id=None, lookback_days=1, force=False):
        if not force:
            from config import conf
            if not conf().get("deep_dream_enabled", True):
                return False
        if not self.llm_model:
            return False
        try:
            with identity_scope(**({"user_id": user_id} if user_id is not None else {})):
                ledger = self._ledger()
                revision = ledger.revision()
                entries = ledger.active_entries()
                if len(entries) < 2:
                    return False
                fingerprint = _digest(entries)
                if not force and fingerprint == self._last_dream_input_hash:
                    return False
                from agent.protocol.models import LLMRequest
                request = LLMRequest(messages=[{"role": "user", "content": json.dumps(entries, ensure_ascii=False)}],
                                     system=DREAM_SYSTEM_PROMPT_ZH, temperature=0.3, stream=False)
                output = _json_object(self._extract_response_text(self.llm_model.call(request)))
                if not output or not isinstance(output.get("merges"), list):
                    return False
                known = {entry["entry_id"] for entry in entries}
                used = set()
                merges = output["merges"]
                for proposal in merges:
                    ids = proposal.get("input_ids", [])
                    if (not isinstance(ids, list) or len(set(ids)) < 2 or len(set(ids)) != len(ids)
                            or not set(ids) <= known or used.intersection(ids)
                            or not isinstance(proposal.get("content"), str) or not proposal["content"].strip()):
                        return False
                    used.update(ids)
                changed = False
                for proposal in merges:
                    request_id = "dream:" + _digest(dict(revision=revision, **proposal))
                    receipt = ledger.merge(request_id, proposal["content"], proposal["input_ids"], revision)
                    if not receipt.get("results") or any(r.get("status") not in ("remembered", "pending_projection")
                                                        for r in receipt["results"]):
                        return False
                    revision = receipt["revision"]
                    changed = True
                self._last_dream_input_hash = _digest(ledger.active_entries())
                return changed
        except Exception as exc:
            logger.warning(f"[DeepDream] 台账合并未完成: {exc}")
            return False

    def _read_main_memory(self, user_id=None):
        from agent.memory.projection import read_memory_text
        return read_memory_text(self.workspace_dir, self.get_main_memory_file(user_id))

    def _write_dream_diary(self, content, user_id=None):
        """梦境不能成为脱离台账依据的独立事实副本。"""
        return False

    def _summarize_messages(self, messages, max_messages=0):
        conversation = self._format_conversation_for_summary(messages, max_messages)
        if self.llm_model and conversation:
            try:
                raw = self._call_llm_for_summary(conversation)
                value = _json_object(raw)
                if (value is not None and isinstance(value.get("context_summary"), str)
                        and isinstance(value.get("entries"), list)):
                    return value
                # 老模型/格式错误的正文最多用于当前上下文，不持久化。
                return dict(context_summary=self._clean_summary_output(raw), entries=[])
            except Exception as exc:
                logger.warning(f"[MemoryFlush] 摘要模型不可用，仅保留上下文: {exc}")
        return dict(context_summary=self._extract_summary_fallback(messages, max_messages), entries=[], _retryable=True)

    def _format_conversation_for_summary(self, messages, max_messages=0):
        selected = messages if max_messages == 0 else messages[-max_messages * 2:]
        evidence = self._ledger().evidence(selected)
        return json.dumps(dict(messages=selected, evidence=evidence), ensure_ascii=False)

    @staticmethod
    def _extract_response_text(response) -> str:
        """
        Extract text from LLM response regardless of format.

        Handles:
        - Generator (MiniMax _handle_sync_response yields Claude-format dicts)
        - Claude format: {"role":"assistant","content":[{"type":"text","text":"..."}]}
        - OpenAI format: {"choices":[{"message":{"content":"..."}}]}
        - OpenAI SDK response object with .choices attribute
        """
        import types

        # Unwrap generator — consume first yielded item
        if isinstance(response, types.GeneratorType):
            try:
                response = next(response)
            except StopIteration:
                return ""

        if not response:
            return ""

        if isinstance(response, dict):
            # Check for error
            if response.get("error"):
                raise RuntimeError(response.get("message", "LLM call failed"))

            # Claude format: content is a list of blocks
            content = response.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text", "")

            # OpenAI format
            choices = response.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")

        # OpenAI SDK response object
        if hasattr(response, "choices") and response.choices:
            return response.choices[0].message.content or ""

        return ""

    def _call_llm_for_summary(self, conversation_text):
        from agent.protocol.models import LLMRequest
        return self._extract_response_text(self.llm_model.call(LLMRequest(
            messages=[{"role": "user", "content": _summarize_user_prompt().format(conversation=conversation_text)}],
            system=_summarize_system_prompt(), temperature=0, stream=False)))

    @staticmethod
    def _extract_summary_fallback(messages, max_messages=0):
        selected = messages if max_messages == 0 else messages[-max_messages * 2:]
        return "\n".join(f"{m.get('role', '?')}: {MemoryFlushManager._extract_text_from_content(m.get('content', ''))[:500]}"
                         for m in selected)[-10000:]

    @staticmethod
    def _extract_text_from_content(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        return ""

def create_memory_files_if_needed(workspace_dir: Path, user_id: Optional[str] = None):
    """
    Create essential memory files if they don't exist.
    Only creates MEMORY.md; daily files are created lazily on first write.
    
    Args:
        workspace_dir: Workspace directory
        user_id: Optional user ID for user-specific files
    """
    memory_dir = workspace_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    
    # Create main MEMORY.md in workspace root (always needed for bootstrap)
    if user_id:
        user_dir = memory_dir / "users" / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        main_memory = user_dir / "MEMORY.md"
    else:
        main_memory = Path(workspace_dir) / "MEMORY.md"
    
    if not main_memory.exists():
        main_memory.write_text("")


def ensure_daily_memory_file(workspace_dir: Path, user_id: Optional[str] = None) -> Path:
    """
    Ensure today's daily memory file exists, creating it only when actually needed.
    Called lazily before first write to daily memory.
    
    Args:
        workspace_dir: Workspace directory
        user_id: Optional user ID for user-specific files
        
    Returns:
        Path to today's memory file
    """
    memory_dir = workspace_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    
    today = datetime.now().strftime("%Y-%m-%d")
    if user_id:
        user_dir = memory_dir / "users" / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        today_memory = user_dir / f"{today}.md"
    else:
        today_memory = memory_dir / f"{today}.md"
    
    if not today_memory.exists():
        today_memory.write_text(
            f"# Daily Memory: {today}\n\n"
        )
    
    return today_memory
