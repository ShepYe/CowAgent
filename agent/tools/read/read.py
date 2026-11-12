"""
Read tool - Read file contents
Supports text files, images (jpg, png, gif, webp), and PDF files
"""

import os
import re
import shlex
import stat
import sys
from typing import Dict, Any, Optional
from pathlib import Path

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.utils.credentials import DENIED_MESSAGE, is_credential_path
from agent.tools.utils.file_state import note_read
from agent.tools.utils.truncate import truncate_head, format_size, DEFAULT_MAX_LINES, DEFAULT_MAX_BYTES
from common.utils import expand_path

EMPTY_FILE_NOTICE = "[File exists but is empty]"

_IS_WIN = sys.platform == "win32"

# PowerShell ends a single-quoted string at any of these, not only at the
# ASCII apostrophe, and a doubled one stands for itself.
_PS_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"


def split_lines(content: str) -> list:
    """Split into lines, dropping the empty element after a trailing newline.

    Without this a file ending in "\\n" reports one line too many and renders a
    phantom numbered blank line at the end.
    """
    lines = content.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return lines


PDF_MAX_PAGES_PER_READ = 20


def _parse_page_range(pages, total_pages: int):
    """Parse a PDF ``pages`` argument into an inclusive (first, last) pair.

    Accepts "5", "2-8" and the open-ended "10-". Defaults to the first
    PDF_MAX_PAGES_PER_READ pages so a large document is never parsed whole.
    """
    if total_pages <= 0:
        return 1, 0

    spec = str(pages or "").strip()
    if not spec:
        return 1, min(PDF_MAX_PAGES_PER_READ, total_pages)

    if '-' in spec:
        head, _, tail = spec.partition('-')
        head, tail = head.strip(), tail.strip()
    else:
        head = tail = spec.strip()

    try:
        first = int(head) if head else 1
        last = int(tail) if tail else total_pages
    except ValueError:
        raise ValueError(f'Invalid pages value "{spec}". Use e.g. "3", "1-5" or "10-".')

    if first < 1 or last < first:
        raise ValueError(f'Invalid pages range "{spec}" for a {total_pages}-page document.')
    if first > total_pages:
        raise ValueError(f'Page {first} is beyond the end of the document ({total_pages} pages).')

    last = min(last, total_pages, first + PDF_MAX_PAGES_PER_READ - 1)
    return first, last


def number_lines(text: str, start_line: int) -> str:
    """Prefix each line with its 1-indexed file line number as ``12|code``.

    The compact gutter (no padding) keeps the token cost low while letting the
    model line up read output with grep hits and ask for a precise offset.
    Callers must tell the model these prefixes are display-only.
    """
    return '\n'.join(
        f"{start_line + i}|{line}" for i, line in enumerate(text.split('\n'))
    )


def _long_line_command(path: str, line_number: int) -> str:
    """A bash tool command that prints up to DEFAULT_MAX_BYTES bytes of one line.

    On Windows the bash tool runs cmd.exe, which has no tail or head, so the
    line is read with PowerShell there. It writes raw bytes, so the console
    code page does not matter, and it stops before a character the limit would
    cut, so the output is valid UTF-8 and stays within the bash tool's limit.
    """
    if not _IS_WIN:
        return f"tail -n +{line_number} -- {shlex.quote(path)} | head -c {DEFAULT_MAX_BYTES}"
    literal = "'" + "".join(c * 2 if c in _PS_SINGLE_QUOTES else c for c in path) + "'"
    if "%" in path:
        # cmd.exe expands %NAME% even inside double quotes, so never hand it a %.
        literal = "(" + literal.replace("%", "'+[char]37+'") + ")"
    return (
        'powershell -NoProfile -Command "'
        f"$bytes = [Text.Encoding]::UTF8.GetBytes([string](Get-Content -LiteralPath {literal} -Encoding UTF8"
        f" | Select-Object -Skip {line_number - 1} -First 1)); "
        f"$end = [Math]::Min($bytes.Length, {DEFAULT_MAX_BYTES}); "
        # Step back over UTF-8 continuation bytes so no character is cut.
        "while ($end -lt $bytes.Length -and ($bytes[$end] -band 0xC0) -eq 0x80) { $end-- }; "
        '[Console]::OpenStandardOutput().Write($bytes, 0, $end)"'
    )


class Read(BaseTool):
    """Tool for reading file contents"""
    
    name: str = "read"
    description: str = f"Read or inspect file contents. For text/PDF/Word/Excel/PPT files, returns content (truncated to {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB). Each line is prefixed with its line number as `12|content` - these prefixes are display aids for locating lines and are NOT part of the file, so strip them whenever you reuse the content (in edit's oldText/newText, when writing it elsewhere, or when quoting it to the user). For images/videos/audio, returns metadata only (file info, size, type). Use offset/limit for large text files. 个人附件返回的 revision_id 标识本次读取的内容版本；expected_current_revision 标识资料当前发布版本（可能为空），传给 knowledge_ingest 时使用该字段。PDF 的 offset/limit 在 pages 筛选后的显示片段内续读，续读时保持 pages 不变。"
    
    params: dict = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read. IMPORTANT: Relative paths are based on workspace directory. To access files outside workspace, use absolute paths starting with ~ or /."
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed, optional). Use negative values to read from end (e.g. -20 for last 20 lines)"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read (optional)"
            },
            "pages": {
                "type": "string",
                "description": f"PDF物理页／Word或PPT阅读副本页范围，如3、1-5、10-。默认前{PDF_MAX_PAGES_PER_READ}页；实际媒体批次受预算约束，按next_pages继续。PPT原件slide位置见source_locations。"
            },
            "region": {
                "type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                "description": "局部放大补读：归一化[left,top,right,bottom]，左上为原点。须同时用pages指定单页；PDF为原件页，Word/PPT为阅读副本页。不替代整页覆盖。"
            },
            "worksheet": {"type": "string", "description": "XLSX精确工作表名，可缩小嵌图和公式结构回执。"},
            "objects": {"type": "array", "items": {"type": "string"},
                        "description": "XLSX局部看图：先read取得spreadsheet.images的object_key，再显式选择这些键。空选择只返回结构；图表不重算、不当作已渲染。"},
            "parse_id": {"type": "string", "description": "显式回读旧解析稿与ordinal，或旧视觉批次续处理时使用；原生PDF首次及按pages续读不传此字段。"},
            "visual": {"type": "boolean", "description": "默认true，能力允许时直接提供原生PDF；原生读取只传path/pages，不加offset/limit/parse_id。false禁止新增媒体及视觉调用，仅返回文字／已有缓存。"}
        },
        "required": ["path"]
    }
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.cwd = self.config.get("cwd", os.getcwd())
        
        # File type categories
        self.image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico'}
        self.video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.webm', '.m4v'}
        self.audio_extensions = {'.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.wma'}
        self.binary_extensions = {'.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.db', '.sqlite'}
        self.archive_extensions = {'.zip', '.tar', '.gz', '.rar', '.7z', '.bz2', '.xz'}
        self.pdf_extensions = {'.pdf'}
        self.office_extensions = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}

        # Readable text formats (will be read with truncation)
        self.text_extensions = {
            '.txt', '.md', '.markdown', '.rst', '.log', '.csv', '.tsv', '.json', '.xml', '.yaml', '.yml',
            '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp', '.go', '.rs', '.rb', '.php',
            '.html', '.css', '.scss', '.sass', '.less', '.vue', '.jsx', '.tsx',
            '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
            '.sql', '.r', '.m', '.swift', '.kt', '.scala', '.clj', '.erl', '.ex',
            '.dockerfile', '.makefile', '.cmake', '.gradle', '.properties', '.ini', '.conf', '.cfg',
        }
    
    def execute(self, args: Dict[str, Any]) -> ToolResult:
        """
        Execute file read operation
        
        :param args: Contains file path and optional offset/limit parameters
        :return: File content or error message
        """
        # Support 'location' as alias for 'path' (LLM may use it from skill listing)
        path = args.get("path", "") or args.get("location", "")
        path = path.strip() if isinstance(path, str) else ""
        offset = args.get("offset")
        limit = args.get("limit")
        pages = args.get("pages")
        region = args.get("region")
        if region is not None and (args.get('worksheet') is not None or args.get('objects') is not None):
            return ToolResult.fail(dict(code='invalid_request', message='region与XLSX对象选择不可混用'))
        if region is not None and (args.get('visual', True) is not True or args.get('parse_id')
                or offset is not None or limit is not None or not isinstance(pages, str)
                or not re.fullmatch(r'[1-9]\d*', pages)):
            return ToolResult.fail(dict(code='invalid_request', message='局部补读须指定单页pages，且visual=true、不带offset/limit/parse_id'))

        if not path:
            return ToolResult.fail("Error: path parameter is required")
        
        # Resolve path
        absolute_path = self._resolve_path(path)

        # Reached only once the path already missed, so nothing that resolves
        # today changes: a "knowledge/..." miss is retried under the shared
        # root before giving up. Runs ahead of the credential check below so
        # whatever is finally read is still screened by it.
        if not os.path.exists(absolute_path):
            shared_page = self._shared_knowledge_path(path)
            if shared_page:
                absolute_path = shared_page

        from agent.knowledge.personal_assistant import guard_path
        try:
            guard_path(absolute_path)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        if any(parent.name in ("wiki", "managed") and parent.parent.name == "knowledge" for parent in Path(absolute_path).resolve().parents):
            from agent.knowledge.purpose import note_source_evidence
            note_source_evidence()

        # Security check: block credential files and their aliases.
        # See issue #2913 (/proc/self/environ bypass) and #2863 (scope).
        if self._is_credential_path(absolute_path):
            return ToolResult.fail(DENIED_MESSAGE)

        attachment_result = self._read_task_attachment(absolute_path, offset, limit, pages,
            visual=args.get("visual", True), parse_id=args.get("parse_id"), region=region,
            worksheet=args.get('worksheet'), objects=args.get('objects'))
        if attachment_result is not None:
            return attachment_result
        
        # Check if file exists. Both misses below name the tool that answers
        # the question instead - otherwise the model tends to guess at nearby
        # paths and burn turns on more failed reads.
        if not os.path.exists(absolute_path):
            hint = "Hint: use search_files with target='files' to locate it by name."
            # Provide helpful hint if using relative path
            if not os.path.isabs(path) and not path.startswith('~'):
                return ToolResult.fail(
                    f"Error: File not found: {path}\n"
                    f"Resolved to: {absolute_path}\n"
                    f"Hint: Relative paths are based on workspace ({self.cwd}). For files outside workspace, use absolute paths.\n"
                    f"{hint}"
                )
            return ToolResult.fail(f"Error: File not found: {path}\n{hint}")

        if os.path.isdir(absolute_path):
            return ToolResult.fail(
                f"Error: {path} is a directory, not a file. "
                f"Use the ls tool to list what is inside it."
            )
        
        # A FIFO has size zero yet open/read waits indefinitely for a writer.
        # Inspect the resolved target before dispatching to any content parser.
        try:
            mode = os.stat(absolute_path).st_mode
        except OSError as exc:
            return ToolResult.fail(f"Error inspecting file: {exc}")
        if not stat.S_ISREG(mode):
            return ToolResult.fail(f"Error: {path} is not a regular file")

        # Check if readable
        if not os.access(absolute_path, os.R_OK):
            return ToolResult.fail(f"Error: File is not readable: {path}")
        
        # Check file type
        file_ext = Path(absolute_path).suffix.lower()
        file_size = os.path.getsize(absolute_path)
        if file_ext != '.xlsx' and (args.get('worksheet') is not None or args.get('objects') is not None):
            return ToolResult.fail(dict(code='invalid_request', message='worksheet/objects仅用于XLSX'))
        
        # Check if image - return metadata for sending
        if file_ext in self.image_extensions:
            return self._read_image(absolute_path, file_ext)
        
        # Check if video/audio/binary/archive - return metadata only
        if file_ext in self.video_extensions:
            return self._return_file_metadata(absolute_path, "video", file_size)
        if file_ext in self.audio_extensions:
            return self._return_file_metadata(absolute_path, "audio", file_size)
        if file_ext in self.binary_extensions or file_ext in self.archive_extensions:
            return self._return_file_metadata(absolute_path, "binary", file_size)
        
        # Check if PDF
        if file_ext in self.pdf_extensions:
            from agent.knowledge.reading_flow import enabled
            if (enabled(self.config) and args.get('visual', True) is True and not args.get('parse_id')
                    and offset is None and limit is None):
                try:
                    native = self._read_general_native_pdf(absolute_path, pages, region=region)
                except Exception as exc:
                    return ToolResult.fail(dict(code=getattr(exc, 'code', 'reading_failed'), message=str(exc)))
                if native is not None:
                    return native
            if region is not None:
                return ToolResult.fail(dict(code='attachment_unsupported', message='当前配置尚未支持原生PDF局部补读'))
            return self._read_pdf(absolute_path, path, offset, limit, pages)

        # Check if Office document (.docx, .xlsx, .pptx, etc.)
        if file_ext in self.office_extensions:
            from agent.knowledge.reading_flow import office_enabled
            if (office_enabled(self.config, absolute_path) and args.get('visual', True) is True
                    and not args.get('parse_id') and offset is None and limit is None):
                try:
                    native = self._read_general_native_office(absolute_path, pages, region=region)
                    if native is not None:
                        return native
                except Exception as exc:
                    return ToolResult.fail(dict(code=getattr(exc, 'code', 'conversion_failed'), message=str(exc)))
            if region is not None:
                return ToolResult.fail(dict(code='attachment_unsupported', message='当前格式尚未支持局部补读'))
            text_result = self._read_office(absolute_path, path, file_ext, offset, limit)
            if (file_ext == '.xlsx' and text_result.status == 'success'
                    and (offset is None and limit is None or args.get('worksheet') is not None or args.get('objects') is not None)):
                try:
                    spreadsheet, _ = self._spreadsheet_read(absolute_path, visual=args.get('visual', True),
                        worksheet=args.get('worksheet'), objects=args.get('objects'))
                    return ToolResult.success(dict(text_result.result, spreadsheet=spreadsheet.result), media=spreadsheet.media)
                except Exception as exc:
                    return ToolResult.fail(dict(code=getattr(exc, 'code', 'parse_failed'), message=str(exc)))
            return text_result

        if region is not None:
            return ToolResult.fail(dict(code='attachment_unsupported', message='局部补读仅支持已启用的PDF/Word/PPT阅读副本'))
        # Read text file (with truncation for large files)
        return self._read_text(absolute_path, path, offset, limit)
    
    def _read_task_attachment(self, absolute_path, offset, limit, pages=None, *, visual=True, parse_id=None, region=None,
                              worksheet=None, objects=None):
        """Read authorized hash blobs using their registered original filename.

        Extraction remains complete; only the returned source units are paged.
        This never decides long-term purpose or publishes a knowledge page.
        """
        from common.personal_tasks import current_task_id, TaskStore
        task_id = current_task_id()
        if not task_id:
            return None
        try:
            native_window = offset is None and limit is None
            from common.runtime_identity import current_identity
            from agent.knowledge.personal_assistant import SourceService
            import json
            store = self.config.get("task_store") or TaskStore()
            owner = current_identity().user_id
            task = store.get(owner, task_id)
            resolved = Path(absolute_path).resolve()
            entry = next((a for a in task["attachments"] if Path(a["path"]).resolve() == resolved), None)
            if entry is None:
                if resolved.is_relative_to((store.root / "originals").resolve()):
                    return ToolResult.fail({"code": "forbidden", "message": "附件不属于当前接收任务"})
                return None
            attachment = store.attachment(owner, task_id, entry["attachment_id"])
            if task.get('reading_publication') and task['status'] == 'succeeded':
                # 任务完成后的复用不再展开附件；出处通过既有发布回读守卫验证。
                from agent.knowledge.wiki import WikiService
                workspace = self.config.get('workspace_root') or self.cwd
                refs = task.get('result_refs', [])
                searchable = bool(refs)
                for ref in refs:
                    if ref.get('publish_id'):
                        try:
                            WikiService(workspace, owner).read_reference(ref['publish_id'])
                        except (ValueError, OSError):
                            searchable = False
                return ToolResult.success(dict(task_id=task_id, reading_state='completed',
                    media_attached=False, result_refs=refs,
                    next_action='复用已发布主题及出处；目标变化须建立新任务。',
                    published=True, searchable=searchable))
            workspace = self.config.get("workspace_root") or self.cwd
            service = SourceService(workspace, owner)
            directive = task.get("payload", {}).get("reorganize") or task.get("payload", {}).get("source_action")
            document_id = None
            if directive:
                from agent.knowledge.parser import SourceError
                document_id = directive["document_id"]
                revision = service.registered_revision(document_id, directive["revision_id"])
                checkpoint = task.get("source_action_checkpoint") or {}
                expected_state = (checkpoint.get("state") if task.get("payload", {}).get("source_action")
                                  else directive.get("expected_state"))
                if (type(expected_state) is not int or service.get(document_id)["state"] != expected_state):
                    raise SourceError("version_conflict", "来源状态已变化，不能重绑原件")
                if revision["sha256"] != attachment["sha256"]:
                    raise SourceError("version_conflict", "任务附件与已登记原件不符")
            receive_args = dict(filename=attachment["filename"],
                request_id=f"{task_id}:{attachment['attachment_id']}", origin=task["origin"],
                document_id=document_id)
            if document_id is None:
                receive_args.pop("document_id")
            receipt = service.receive(attachment["path"], **receive_args)
            if Path(attachment['filename']).suffix.lower() != '.xlsx' and (worksheet is not None or objects is not None):
                return ToolResult.fail(dict(code='invalid_request', message='worksheet/objects仅用于XLSX'))
            is_pdf = Path(attachment["filename"]).suffix.lower() in self.pdf_extensions
            page_range = None
            if is_pdf:
                from pypdf import PdfReader
                total_pages = len(PdfReader(attachment["path"]).pages)
                page_range = _parse_page_range(pages, total_pages)
            offset = 1 if offset is None else offset
            limit = 20 if limit is None else limit
            if type(offset) is not int or type(limit) is not int or limit <= 0:
                return ToolResult.fail({"code": "invalid_request", "message": "offset/limit 须为整数且 limit 大于零"})
            if type(visual) is not bool or (parse_id is not None and not isinstance(parse_id, str)):
                return ToolResult.fail({"code": "invalid_request", "message": "visual 须为布尔值，parse_id 须为字符串"})
            from agent.knowledge.reading_flow import enabled, office_enabled
            if (office_enabled(self.config, attachment['filename']) and visual and not parse_id
                    and native_window and task.get('status') == 'running'):
                from agent.knowledge.reading_capabilities import ensure_pdf_capability
                if ensure_pdf_capability(workspace, self.model, task['config_revision']):
                    return self._read_native_office(store, task, workspace, receipt, pages, region=region)
            if is_pdf and visual and not parse_id and native_window and enabled(self.config) and task.get('status') == 'running':
                from agent.knowledge.reading_capabilities import ensure_pdf_capability
                if ensure_pdf_capability(workspace, self.model, task['config_revision']):
                    return self._read_native_pdf(store, task, workspace, receipt, page_range, region=region)
            if region is not None:
                return ToolResult.fail(dict(code='attachment_unsupported', message='当前格式或配置尚未支持局部补读'))
            if is_pdf and not parse_id:
                parse_id = (task.get("source_read_parses") or {}).get(receipt["revision_id"])
            parse_args = {"base_parse_id": parse_id} if parse_id else {}
            spreadsheet = None
            if (Path(attachment['filename']).suffix.lower() == '.xlsx' and not parse_id
                    and (native_window or worksheet is not None or objects is not None)):
                spreadsheet, materials = self._spreadsheet_read(absolute_path, visual=visual,
                    worksheet=worksheet, objects=objects, task_context=(store, task, receipt))
                parse_args.update(spreadsheet_materials=materials, materials_root=workspace)
            parsed = service.parse(receipt["revision_id"], **parse_args)
            if visual and is_pdf and parsed.get("visual_coverage"):
                from agent.tools.vision.vision import Vision
                vision = Vision(dict(self.config, cwd=str(workspace)))
                vision.model = self.model
                parsed = service.parse(receipt["revision_id"], visual=True, vision=vision,
                    visual_budget=self.config.get("visual_budget"), config_revision=task.get("config_revision"),
                    base_parse_id=parsed["parse_id"])
            if is_pdf and task.get("status") == "running":
                # Persist the baseline for later page windows and task recovery;
                # cancellation/attempt changes must reject a late read result.
                with store.commit_guard(owner, task_id, task["attempt"]):
                    current = store.get(owner, task_id)
                    read_parses = dict(current.get("source_read_parses") or {})
                    read_parses[receipt["revision_id"]] = parsed["parse_id"]
                    store._change(owner, task_id, task["attempt"], {"running"}, source_read_parses=read_parses)
            if directive and service.get(document_id)["state"] != expected_state:
                from agent.knowledge.parser import SourceError
                raise SourceError("version_conflict", "阅读期间来源状态已变化")
            # Preserve full locations while splitting very large physical pages
            # into display fragments; text offsets make every fragment resumable.
            units = []
            for ordinal, unit in enumerate(parsed["units"]):
                if page_range is not None and not (
                    page_range[0] <= unit["location"]["physical_page"] <= page_range[1]
                ):
                    continue
                for begin in range(0, len(unit["text"]), 2000):
                    units.append(dict(unit, text=unit["text"][begin:begin+2000], ordinal=ordinal,
                                      text_start=begin, text_end=min(begin+2000, len(unit["text"]))))
            start = max(0, len(units) + offset) if offset < 0 else max(0, offset-1)
            selected = []
            # Continuation precedes potentially large source/coverage/body data.
            result = dict(has_more=start < len(units), next_offset=start+1 if start < len(units) else None,
                next_pages=None, next_visual_page=None, visual_continuation=None,
                parse_id=parsed["parse_id"], document_id=receipt["document_id"], revision_id=receipt["revision_id"],
                expected_current_revision=service.get(receipt["document_id"])["current"],
                attachment_id=attachment["attachment_id"], filename=attachment["filename"],
                total_units=len(units), offset=start+1,
                published=False, searchable=False, display_fragments=True)
            coverage = parsed.get("visual_coverage", [])
            if page_range is not None:
                first_page, last_page = page_range
                result.update(total_pages=total_pages, pages_read=f"{first_page}-{last_page}",
                              next_pages=f"{last_page+1}-" if last_page < total_pages else None)
                pending_visual = sorted((c for c in coverage
                                         if c["status"] == "pending"),
                                        key=lambda c: (c.get("attempts", 0), c["physical_page"]))
                result["next_visual_page"] = pending_visual[0]["physical_page"] if pending_visual else None
                result["visual_continuation"] = (dict(parse_id=parsed["parse_id"], visual=True,
                    pages=f"{pending_visual[0]['physical_page']}-")
                                                 if pending_visual else None)
                result["visual_coverage_summary"] = dict(
                    total_pages=total_pages, recorded_pages=len(coverage),
                    text_pages=sum(bool(c.get("text")) for c in coverage),
                    rendered_pages=sum(bool(c.get("rendered")) for c in coverage),
                    ocr_pages=sum(bool(c.get("ocr")) for c in coverage),
                    visual_interpretation_pages=sum(bool(c.get("visual_interpretation")) for c in coverage),
                    processed_pages=sum(c.get("status") == "processed" for c in coverage),
                    pending_pages=sum(c.get("status") == "pending" for c in coverage),
                    partial_pages=sum(c.get("status") == "partial" for c in coverage),
                    blank_pages=sum(c.get("status") == "blank" for c in coverage))
                coverage = [c for c in coverage if first_page <= c["physical_page"] <= last_page]
            result["visual_coverage"] = coverage
            result["visual_warnings"] = parsed.get("visual_warnings", [])
            result["units"] = selected
            if spreadsheet is not None:
                result['spreadsheet'] = spreadsheet.result
            # Include metadata and JSON separators in the budget. Leave room
            # below the Agent's 50000-character cutoff so continuation survives.
            budget = min(DEFAULT_MAX_BYTES, 45000)
            def response_bytes():
                return len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))

            def oversized_metadata():
                return ToolResult.fail(dict(code="limit_exceeded", parse_id=parsed["parse_id"],
                    pages_read=result.get("pages_read"), next_offset=start+1,
                    message="当前页窗元数据与片段超过回执预算，请缩小 pages 范围后续读"))

            if response_bytes() > budget:
                return oversized_metadata()
            for unit in units[start:start+min(limit, 200)]:
                selected.append(unit)
                next_index = start + len(selected)
                result.update(has_more=next_index < len(units),
                              next_offset=next_index+1 if next_index < len(units) else None)
                if response_bytes() > budget:
                    selected.pop()
                    if not selected:
                        return oversized_metadata()
                    break
            next_index = start + len(selected)
            result.update(has_more=next_index < len(units),
                          next_offset=next_index+1 if next_index < len(units) else None)
            return ToolResult.success(result, media=spreadsheet.media if spreadsheet is not None else ())
        except Exception as exc:
            return ToolResult.fail({"code": getattr(exc, "code", "invalid_request"), "message": str(exc)})

    def _spreadsheet_read(self, absolute_path, *, visual, worksheet, objects, task_context=None):
        """XLSX 只按显式对象选择附图；公式与对象位置仍由本地原件核验。"""
        import hashlib
        from agent.knowledge.personal_assistant import guard_path, SourceService
        from agent.knowledge.reading_materials import _store
        from agent.knowledge.spreadsheet_materials import prepare_spreadsheet_materials
        from agent.knowledge.spreadsheet_reading_flow import prepare_spreadsheet_read, SpreadsheetReadBudget
        from common.runtime_identity import current_identity
        from common.personal_tasks import current_task_id
        from models.config_revision import capture_config_revision
        workspace = Path(self.config.get('workspace_root') or self.cwd).resolve()
        source = Path(absolute_path).resolve(strict=True)
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        relative = f'.document_reading/spreadsheets/{digest}/source.xlsx'
        _store(workspace, relative, data)
        materials = prepare_spreadsheet_materials(workspace, relative)
        identity, task_id, revision = current_identity(), current_task_id(), capture_config_revision()
        checked_images = {}

        def authorize(context, binding):
            guard_path(source)
            if (self._is_credential_path(str(source)) or current_identity() != identity
                    or current_task_id() != task_id or capture_config_revision() != revision
                    or hashlib.sha256(source.read_bytes()).hexdigest() != digest):
                return False
            if task_context is not None:
                store, task, receipt = task_context
                with store.commit_guard(task['owner_id'], task_id, task['attempt']) as current:
                    if current['config_revision'] != revision:
                        return False
                    service = SourceService(workspace, identity.user_id)
                    state = service.get(receipt['document_id'])
                    latest = service.registered_revision(receipt['document_id'])
                    return state['validity'] == 'active' and latest['revision_id'] == receipt['revision_id']
            return True

        def supports(context, mime):
            if mime not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
                return False
            model = getattr(self, 'model', None)
            meta = getattr(model, 'catalog_model_meta', None)
            if callable(meta) and 'vision' in ((meta() or {}).get('capabilities') or []):
                return True
            bot = getattr(model, 'bot', None)
            per_model = getattr(bot, 'model_supports_vision', None)
            if callable(per_model) and per_model(getattr(model, 'model', '')) is True:
                return True
            if mime not in checked_images:
                from agent.knowledge.image_reading_capabilities import ensure_image_capability
                checked_images[mime] = ensure_image_capability(workspace, model, revision, mime)
            return checked_images[mime]

        result = prepare_spreadsheet_read(workspace, relative, worksheet=worksheet,
            selected_objects=() if objects is None else objects, visual=visual,
            model=getattr(self, 'model', None), media_supports=supports, authorize=authorize,
            budget=SpreadsheetReadBudget(max_receipt_bytes=min(DEFAULT_MAX_BYTES, 40000)))
        result.result.update(long_term_image_evidence_available=False,
            next_action='按工作表和object_key补读图片；图内模型观察尚不支持长期发布，不能以单元格文字出处替代图片观察。')
        return result, materials

    def _read_native_pdf(self, store, task, workspace, receipt, page_range, *, region=None):
        from agent.knowledge.reading_flow import prepare_pdf_read
        task, parsed, materials, registry, provider = prepare_pdf_read(
            store, task, workspace, receipt, self.model, *page_range,
            budget=self.config.get('reading_pdf_budget'), config=self.config)
        return self._native_read_result(store, task, workspace, receipt, parsed, materials, registry, provider, page_range, region=region)

    def _read_native_office(self, store, task, workspace, receipt, pages, *, region=None):
        from agent.knowledge.reading_flow import office_options, prepare_task_target
        from agent.knowledge.office_reading_flow import prepare_office_read
        task = prepare_task_target(store, task, workspace, self.config)
        office = task['source_office_materials'][receipt['revision_id']]['manifest']
        page_range = _parse_page_range(pages, office['page_count'])
        task, parsed, materials, registry, provider = prepare_office_read(
            store, task, workspace, receipt, self.model, *page_range,
            budget=self.config.get('reading_pdf_budget'), **office_options(self.config))
        return self._native_read_result(store, task, workspace, receipt, parsed, materials, registry, provider, page_range, region=region)

    def _native_read_result(self, store, task, workspace, receipt, parsed, materials, registry, provider, page_range, *, region=None):
        from agent.knowledge.personal_assistant import SourceService
        from common.runtime_identity import current_identity
        if region is not None:
            from agent.knowledge.reading_materials import prepare_pdf_region
            materials = prepare_pdf_region(workspace, materials, page_range[0], region)
        record = registry.prepare(task, receipt, parsed, materials)
        if record['attempt'] != task['attempt']:
            record = registry.resume(task, record['reading_id'])
        first, last = page_range
        def select(current):
            matches = [s for s in current['segments'] if any(first <= p <= last for p in s['target_pages'])]
            return next((s for s in matches if s['state'] != 'completed'), matches[0] if matches else None)
        selected = select(record)
        if selected is None:
            from agent.knowledge.parser import SourceError
            raise SourceError('invalid_request', '页窗不属于本次阅读目标')
        while selected.get('error_code') == 'reading_budget_exceeded' or selected['state'] == 'unknown':
            if selected.get('error_code') == 'reading_budget_exceeded':
                record = registry.replan_budget_exceeded(task, record['reading_id'], selected['segment_id'])
            else:
                record = registry.retry_unknown_segment(task, record['reading_id'], selected['segment_id'])
            selected = select(record)
        if selected['state'] == 'completed':
            media = ()
        else:
            from dataclasses import replace
            def on_error(error):
                if error.get('code') == 'reading_budget_exceeded' and error.get('budget_kind') in ('input_bytes', 'input_context'):
                    registry.mark_budget_exceeded(task, record['reading_id'], selected['segment_id'],
                        error_code=error['code'], budget_kind=error['budget_kind'])
            media = (replace(registry.media_reference(task, record['reading_id'], selected['segment_id'],
                identity=current_identity(), provider_identity=provider), on_error=on_error),)
        last_page = max(selected['target_pages'])
        details = {}
        if materials.region_supplement is not None:
            details.update(region=materials.region_supplement['region'], supplemental=True,
                           source_locations=[dict(reading_copy_page=1, location=materials.location(page_range[0]))],
                           coverage_keys=[])
        if materials.derived_source is not None:
            office = materials.derived_source['office_manifest']
            details.update(source_format=office['source_format'],
                source_locations=[dict(reading_copy_page=1 if region is not None else p, location=materials.location(p))
                                  for p in selected['page_map']],
                notes=office['notes'], coverage_gaps=office['coverage_gaps'])
        return ToolResult.success(dict(reading_id=record['reading_id'],
            document_id=receipt['document_id'], revision_id=receipt['revision_id'], parse_id=parsed['parse_id'],
            expected_current_revision=SourceService(workspace).get(receipt['document_id'])['current'],
            segment_id=selected['segment_id'], page_map=selected['page_map'],
            target_pages=selected['target_pages'], total_pages=materials.page_count,
            reading_state=selected['state'], media_attached=bool(media),
            saved_model_output=selected.get('saved_model_output', []),
            next_pages=f'{last_page + 1}-' if last_page < materials.page_count else None,
            has_more=last_page < materials.page_count, published=False, searchable=False,
            next_action='本批图文只在紧接的模型响应提供。先在该响应写出带源位置的主题发现、关键数值、公式和图形关系，'
                '或直接knowledge_ingest提交正文、summary和具体观察；再调用其他工具。不能只声明已读就先查索引。'
                '后续复用本轮已保存输出；有明确缺口再局部补读。'
                if media else '复用已保存观察或候选；有新缺口时明确补读，不自动重传附件。',
            **details), media=media)

    def _read_general_native_office(self, absolute_path, pages, *, region=None):
        import hashlib
        from dataclasses import replace
        from common.personal_tasks import current_task_id
        from agent.knowledge.reading_capabilities import ensure_pdf_capability
        from agent.knowledge.reading_flow import office_options
        from agent.knowledge.office_materials import prepare_office_materials, verify_office_materials
        from agent.knowledge.reading_materials import _store
        from models.config_revision import capture_config_revision
        if current_task_id() is not None:
            return None
        workspace = Path(self.config.get('workspace_root') or self.cwd).resolve()
        if not ensure_pdf_capability(workspace, self.model, capture_config_revision()):
            return None
        original = Path(absolute_path).resolve(strict=True)
        data = original.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        relative = f'.document_reading/office_originals/{digest}/source{original.suffix.lower()}'
        _store(workspace, relative, data)
        office = prepare_office_materials(workspace, relative, **office_options(self.config))
        result = self._read_general_native_pdf(workspace / office.material_relpath, pages, region=region)
        if result is None:
            return None
        references = []
        for ref in result.media:
            def load(context, base=ref.loader):
                from agent.knowledge.personal_assistant import guard_path
                guard_path(original)
                if self._is_credential_path(str(original)) or hashlib.sha256(original.read_bytes()).hexdigest() != digest:
                    raise ValueError('version_conflict')
                verify_office_materials(workspace, office)
                return base(context)
            references.append(replace(ref, loader=load))
        result.result.update(filename=original.name, source_format=office.source_format,
            source_locations=[dict(reading_copy_page=1 if region is not None else p,
                                   location=dict(office.location(p), region=list(region)) if region is not None else office.location(p))
                              for p in result.result['page_map']],
            notes=list(office.notes), coverage_gaps=list(office.coverage_gaps))
        return ToolResult.success(result.result, media=tuple(references))

    def _read_general_native_pdf(self, absolute_path, pages, *, region=None):
        """普通阅读复用材料／媒体能力，不登记长期资料或发布授权。"""
        import hashlib
        import tempfile
        from pypdf import PdfReader
        from agent.knowledge.reading_capabilities import ensure_pdf_capability, capability_identity
        from agent.knowledge.reading_materials import prepare_pdf_materials, _relative
        from agent.protocol.tool_media import ToolMediaReference, ToolMediaContent
        from common.runtime_identity import current_identity
        from common.personal_tasks import current_task_id
        from models.config_revision import capture_config_revision, require_config_revision
        workspace = Path(self.config.get('workspace_root') or self.cwd).resolve()
        if current_task_id() is not None:
            return None
        revision = capture_config_revision()
        if not ensure_pdf_capability(workspace, self.model, revision):
            return None
        provider = capability_identity(self.model, revision)
        identity = current_identity()
        path = Path(absolute_path).resolve(strict=True)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        relative = f'.document_reading/originals/{digest}.pdf'
        dest = workspace / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as stream:
                temp = Path(stream.name)
                stream.write(raw)
            try:
                os.replace(temp, dest)
            finally:
                temp.unlink(missing_ok=True)
        total = len(PdfReader(path).pages)
        first, last = _parse_page_range(pages, total)
        materials = prepare_pdf_materials(workspace, relative, document_id='general-' + digest,
            revision_id=digest, parse_id='general-pdf-v1', config_revision=revision,
            target_pages=range(first, last + 1), budget=self.config.get('reading_pdf_budget'))
        if region is not None:
            from agent.knowledge.reading_materials import prepare_pdf_region
            materials = prepare_pdf_region(workspace, materials, first, region)
        segment = materials.segments[0]

        def supports(context):
            return (context.get('protocol') == 'responses' and context.get('model') == provider['model']
                and context.get('identity') == identity and context.get('config_revision') == revision
                and context.get('task_id') is None
                and hashlib.sha256(str(context.get('api_base') or '').rstrip('/').encode()).hexdigest() == provider['endpoint_hash'])

        def load(context):
            if not supports(context):
                raise ValueError('attachment_unsupported')
            require_config_revision(revision)
            from agent.knowledge.personal_assistant import guard_path
            guard_path(path)
            if self._is_credential_path(str(path)) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('version_conflict')
            data = _relative(workspace, segment.material_relpath).read_bytes()
            if hashlib.sha256(data).hexdigest() != segment.material_hash:
                raise ValueError('storage_failed')
            return ToolMediaContent('application/pdf', data)

        last_page = max(segment.target_pages)
        return ToolResult.success(dict(filename=path.name, total_pages=total,
            page_map=list(segment.page_map), target_pages=list(segment.target_pages),
            media_attached=True, published=False, searchable=False,
            has_more=last_page < total, next_pages=f'{last_page + 1}-' if last_page < total else None,
            **(dict(region=list(region), supplemental=True, coverage_keys=[],
                    source_locations=[dict(reading_copy_page=1, location=materials.location(first))]) if region is not None else {})),
            media=(ToolMediaReference(load, supports),))

    def _shared_knowledge_path(self, path: str) -> Optional[str]:
        """An existing knowledge page under the shared root, or None.

        ``state_dir`` sends an Agent with no ``knowledge/`` of its own to the
        shared root, which is outside the workspace relative paths resolve
        against here. That is the spelling memory_search results and the links
        in index.md use, so accept it rather than making the model translate
        it. Anything else returns None, keeping this to one extra lookup on a
        path that has already missed.
        """
        if os.path.isabs(path) or path.startswith('~'):
            return None
        parts = Path(path).parts
        if len(parts) < 2 or parts[0] != 'knowledge':
            return None
        try:
            from common import state_dir
            root = os.path.realpath(str(state_dir.knowledge_dir(base=self.cwd)))
        except Exception:
            return None
        candidate = os.path.realpath(os.path.join(root, *parts[1:]))
        # "knowledge/../.." must not turn this into a general way to name files
        # elsewhere; the fallback only ever means a page under the shared root.
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate if os.path.exists(candidate) else None

    def _resolve_path(self, path: str) -> str:
        """
        Resolve path to absolute path
        
        :param path: Relative or absolute path
        :return: Absolute path
        """
        # Expand ~ to user home directory
        path = expand_path(path)
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(self.cwd, path))

    @staticmethod
    def _paginate(all_lines, offset, limit, display_path):
        """Slice, truncate and number a list of lines for presentation.

        Shared by the text, PDF and Office readers so that offset/limit,
        truncation and the continuation hints behave identically everywhere -
        previously each reader reimplemented this and they had drifted apart
        (only the text reader handled negative offsets correctly).

        :return: (result_dict, error_message) - exactly one is not None.
        """
        total_lines = len(all_lines)

        if total_lines == 0:
            return {
                "content": EMPTY_FILE_NOTICE,
                "total_lines": 0,
                "start_line": 0,
                "output_lines": 0,
                "is_empty": True,
            }, None

        start_line = 0
        if offset is not None:
            if offset < 0:
                # -20 means "the last 20 lines".
                start_line = max(0, total_lines + offset)
            else:
                start_line = max(0, offset - 1)
                if start_line >= total_lines:
                    return None, (
                        f"Error: Offset {offset} is beyond end of file "
                        f"({total_lines} lines total)"
                    )

        end_line = total_lines
        user_limited = False
        if limit is not None:
            # limit=0 used to return an empty page with a "continue" hint that
            # pointed back at the same line; a negative one sliced from the end.
            if limit <= 0:
                return None, (
                    f"Error: limit must be a positive number of lines (got {limit}). "
                    f"Omit limit to read the whole file, or pass limit=20 to read 20 lines."
                )
            end_line = min(start_line + limit, total_lines)
            user_limited = True

        selected = '\n'.join(all_lines[start_line:end_line])
        truncation = truncate_head(selected)
        start_display = start_line + 1

        if truncation.first_line_exceeds_limit:
            size = format_size(len(all_lines[start_line].encode('utf-8')))
            return {
                "content": (
                    f"[Line {start_display} is {size}, exceeds "
                    f"{format_size(DEFAULT_MAX_BYTES)} limit. Use bash tool to read: "
                    f"{_long_line_command(expand_path(display_path), start_display)}]"
                ),
                "total_lines": total_lines,
                "start_line": start_display,
                "output_lines": 0,
                "details": {"truncation": truncation.to_dict()},
            }, None

        output_text = number_lines(truncation.content, start_display)
        details = {}

        if truncation.truncated:
            end_display = start_display + truncation.output_lines - 1
            limit_note = (
                "" if truncation.truncated_by == "lines"
                else f" ({format_size(DEFAULT_MAX_BYTES)} limit)"
            )
            output_text += (
                f"\n\n[Showing lines {start_display}-{end_display} of {total_lines}"
                f"{limit_note}. Use offset={end_display + 1} to continue.]"
            )
            details["truncation"] = truncation.to_dict()
        elif user_limited and end_line < total_lines:
            output_text += (
                f"\n\n[{total_lines - end_line} more lines in file. "
                f"Use offset={end_line + 1} to continue.]"
            )

        result = {
            "content": output_text,
            "total_lines": total_lines,
            "start_line": start_display,
            "output_lines": truncation.output_lines,
        }
        if details:
            result["details"] = details
        return result, None

    def _is_credential_path(self, absolute_path: str) -> bool:
        """Shared guard; see agent.tools.utils.credentials.is_credential_path."""
        return is_credential_path(absolute_path)

    def _return_file_metadata(self, absolute_path: str, file_type: str, file_size: int) -> ToolResult:
        """
        Return file metadata for non-readable files (video, audio, binary, etc.)
        
        :param absolute_path: Absolute path to the file
        :param file_type: Type of file (video, audio, binary, etc.)
        :param file_size: File size in bytes
        :return: File metadata
        """
        file_name = Path(absolute_path).name
        file_ext = Path(absolute_path).suffix.lower()
        
        # Determine MIME type
        mime_types = {
            # Video
            '.mp4': 'video/mp4', '.avi': 'video/x-msvideo', '.mov': 'video/quicktime',
            '.mkv': 'video/x-matroska', '.webm': 'video/webm',
            # Audio
            '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
            '.m4a': 'audio/mp4', '.flac': 'audio/flac',
            # Binary
            '.zip': 'application/zip', '.tar': 'application/x-tar',
            '.gz': 'application/gzip', '.rar': 'application/x-rar-compressed',
        }
        mime_type = mime_types.get(file_ext, 'application/octet-stream')
        
        result = {
            "type": f"{file_type}_metadata",
            "file_type": file_type,
            "path": absolute_path,
            "file_name": file_name,
            "mime_type": mime_type,
            "size": file_size,
            "size_formatted": format_size(file_size),
            "message": f"{file_type.capitalize()} 文件: {file_name} ({format_size(file_size)})\n提示: 如果需要发送此文件，请使用 send 工具。"
        }
        
        return ToolResult.success(result)
    
    def _read_image(self, absolute_path: str, file_ext: str) -> ToolResult:
        """
        Read image file - always return metadata only (images should be sent, not read into context)
        
        :param absolute_path: Absolute path to the image file
        :param file_ext: File extension
        :return: Result containing image metadata for sending
        """
        try:
            # Get file size
            file_size = os.path.getsize(absolute_path)
            
            # Determine MIME type
            mime_type_map = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }
            mime_type = mime_type_map.get(file_ext, 'image/jpeg')
            
            # Return metadata for images (NOT file_to_send - use send tool to actually send)
            result = {
                "type": "image_metadata",
                "file_type": "image",
                "path": absolute_path,
                "mime_type": mime_type,
                "size": file_size,
                "size_formatted": format_size(file_size),
                "message": f"图片文件: {Path(absolute_path).name} ({format_size(file_size)})\n提示: 如果需要发送此图片，请使用 send 工具。"
            }
            
            return ToolResult.success(result)
            
        except Exception as e:
            return ToolResult.fail(f"Error reading image file: {str(e)}")
    
    def _read_text(self, absolute_path: str, display_path: str, offset: int = None, limit: int = None) -> ToolResult:
        """
        Read text file
        
        :param absolute_path: Absolute path to the file
        :param display_path: Path to display
        :param offset: Starting line number (1-indexed)
        :param limit: Maximum number of lines to read
        :return: File content or error message
        """
        try:
            # Check file size first
            file_size = os.path.getsize(absolute_path)
            MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
            
            if file_size > MAX_FILE_SIZE:
                # File too large, return metadata only
                return ToolResult.success({
                    "type": "file_to_send",
                    "file_type": "document",
                    "path": absolute_path,
                    "size": file_size,
                    "size_formatted": format_size(file_size),
                    "message": f"文件过大 ({format_size(file_size)} > 50MB)，无法读取内容。文件路径: {absolute_path}"
                })
            
            # Read file (utf-8-sig strips BOM automatically on Windows)
            # Note: Truncation is unified via truncate_head (DEFAULT_MAX_LINES / DEFAULT_MAX_BYTES)
            # so that offset/limit can paginate the entire file correctly.
            with open(absolute_path, 'r', encoding='utf-8-sig') as f:
                content = f.read()

            result, error = self._paginate(
                split_lines(content), offset, limit, display_path
            )
            if error:
                return ToolResult.fail(error)

            # Record the read so edit/write can warn if the file changes later.
            note_read(absolute_path)
            return ToolResult.success(result)
            
        except UnicodeDecodeError:
            return ToolResult.fail(f"Error: File is not a valid text file (encoding error): {display_path}")
        except Exception as e:
            return ToolResult.fail(f"Error reading file: {str(e)}")
    
    def _read_office(self, absolute_path: str, display_path: str, file_ext: str,
                     offset: int = None, limit: int = None) -> ToolResult:
        """Read Office documents (.docx, .xlsx, .pptx) using python-docx / openpyxl / python-pptx."""
        try:
            text = self._extract_office_text(absolute_path, file_ext)
        except ImportError as e:
            return ToolResult.fail(str(e))
        except Exception as e:
            return ToolResult.fail(f"Error reading Office document: {e}")

        if not text or not text.strip():
            return ToolResult.success({
                "content": f"[Office file {Path(absolute_path).name}: no text content could be extracted]",
            })

        result, error = self._paginate(
            split_lines(text), offset, limit, display_path
        )
        if error:
            return ToolResult.fail(error)

        note_read(absolute_path)
        return ToolResult.success(result)

    @staticmethod
    def _extract_office_text(absolute_path: str, file_ext: str) -> str:
        """Extract plain text from an Office document."""
        if file_ext in ('.docx', '.doc'):
            try:
                from docx import Document
            except ImportError:
                raise ImportError("Error: python-docx library not installed. Install with: pip install python-docx")
            from common.office_text import iter_docx_body_text
            doc = Document(absolute_path)
            return '\n'.join(iter_docx_body_text(doc))

        if file_ext in ('.xlsx', '.xls'):
            try:
                from openpyxl import load_workbook
            except ImportError:
                raise ImportError("Error: openpyxl library not installed. Install with: pip install openpyxl")
            from common.office_text import spreadsheet_sheets
            parts = []
            with spreadsheet_sheets(absolute_path, load_workbook) as sheets:
                for name, rows in sheets:
                    parts.append(f"--- Sheet: {name} ---")
                    parts.extend('\t'.join(row) for row in rows)
            return '\n'.join(parts)

        if file_ext in ('.pptx', '.ppt'):
            try:
                from pptx import Presentation
            except ImportError:
                raise ImportError("Error: python-pptx library not installed. Install with: pip install python-pptx")
            from common.office_text import iter_pptx_shape_text
            prs = Presentation(absolute_path)
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                parts.append(f"--- Slide {i} ---")
                parts.extend(iter_pptx_shape_text(slide.shapes))
            return '\n'.join(parts)

        return ""

    def _read_pdf(self, absolute_path: str, display_path: str, offset: int = None,
                  limit: int = None, pages: str = None) -> ToolResult:
        """
        Read PDF file content
        
        :param absolute_path: Absolute path to the file
        :param display_path: Path to display
        :param offset: Starting line number (1-indexed)
        :param limit: Maximum number of lines to read
        :return: PDF text content or error message
        """
        try:
            # Try to import pypdf
            try:
                from pypdf import PdfReader
            except ImportError:
                return ToolResult.fail(
                    "Error: pypdf library not installed. Install with: pip install pypdf"
                )
            
            # Read PDF
            reader = PdfReader(absolute_path)
            total_pages = len(reader.pages)

            try:
                first_page, last_page = _parse_page_range(pages, total_pages)
            except ValueError as e:
                return ToolResult.fail(f"Error: {e}")

            # Only extract the requested window; a large PDF is expensive to
            # parse in full and the model rarely needs every page.
            text_parts = []
            for page_num in range(first_page, last_page + 1):
                page_text = reader.pages[page_num - 1].extract_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {page_num} ---\n{page_text}")

            if not text_parts:
                scope = (
                    f"pages {first_page}-{last_page} of {total_pages}"
                    if (first_page, last_page) != (1, total_pages)
                    else f"{total_pages} pages"
                )
                return ToolResult.success({
                    "content": f"[PDF file with {scope}, but no text content could be extracted]",
                    "total_pages": total_pages,
                    "message": "PDF may contain only images or be encrypted"
                })

            result, error = self._paginate(
                split_lines("\n\n".join(text_parts)), offset, limit, display_path
            )
            if error:
                return ToolResult.fail(error)

            result["total_pages"] = total_pages
            result["pages_read"] = f"{first_page}-{last_page}"
            if last_page < total_pages:
                result["content"] += (
                    f"\n\n[Read pages {first_page}-{last_page} of {total_pages}. "
                    f"Use pages=\"{last_page + 1}-\" to continue.]"
                )

            note_read(absolute_path)
            return ToolResult.success(result)
            
        except Exception as e:
            return ToolResult.fail(f"Error reading PDF file: {str(e)}")
