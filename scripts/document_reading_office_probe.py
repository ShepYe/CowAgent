"""O-01/P-02 离线探针；不作为产品转换器，不接受用户文件。"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "tmp/document-reading-office"
OFFICE_NAMES = {"WINWORD.EXE", "POWERPNT.EXE"}


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_pdf(path, evidence):
    """导出调用成功不等于覆盖／映射通过；这里只核验自生成样本。"""
    import fitz

    with fitz.open(path) as pdf:
        texts = [page.get_text() for page in pdf]
        evidence["pdf_checks"] = {
            "pages": len(pdf), "fonts": sorted({f[3] for page in pdf for f in page.get_fonts()}),
            "image_counts": [len(page.get_images()) for page in pdf],
            "font_warning": "仅列实际 PDF 字体，未证明无字体替换",
        }
        if "layers" in evidence:
            evidence["pdf_checks"].update(
                deleted_revision_absent="旧预算" not in "".join(texts),
                comment_layer_absent="批注附加层" not in "".join(texts),
                final_revision_present="新预算 42 dB" in "".join(texts))
        else:
            mapping = evidence.get("mapping", [])
            verified = len(mapping) == len(pdf) and all(
                f"幻灯片 {item['slide']}" in texts[i] for i, item in enumerate(mapping))
            evidence["pdf_checks"]["slide_mapping_verified"] = verified
            if not verified:
                evidence["status"] = "converted_with_gap"
                evidence["coverage_gap"] = "隐藏页未导出或页序不符；候选 PDF 页映射不可使用"
                for item in mapping:
                    item["candidate_reading_pdf_page"] = item.pop("reading_pdf_page", None)


def preflight(path):
    """保守拒绝活动包；不是任意 OOXML 的完备安全解析器。"""
    reasons = set()
    if Path(path).suffix.lower() not in {".docx", ".pptx"}:
        return ["unsupported_format"]
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                reasons.add("duplicate_parts")
            if sum(i.file_size for i in archive.infolist()) > 32 * 1024 * 1024:
                return ["package_limit"]
            for name in names:
                lower = name.lower()
                if any(x in lower for x in ("vbaproject", "vba/", "activex", "embeddings/", "customui", "externallink")):
                    reasons.add("active_content")
                if lower.endswith((".xml", ".rels")):
                    raw = archive.read(name)
                    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                        reasons.add("xml_entity")
                        continue
                    tree = ET.fromstring(raw)
                    for node in tree.iter():
                        tag = node.tag.rsplit("}", 1)[-1].lower()
                        if tag in {"fldsimple", "fldchar", "instrtext", "altchunk", "oleobj", "oleobject", "attachedtemplate"}:
                            reasons.add("dynamic_field_or_object")
                        if tag == "relationship" and node.get("TargetMode", "").lower() == "external":
                            reasons.add("external_relationship")
                        if any("macroenabled" in value.lower() or "vbaproject" in value.lower()
                               for value in node.attrib.values()):
                            reasons.add("active_content")
    except (OSError, zipfile.BadZipFile, ET.ParseError):
        reasons.add("invalid_package")
    return sorted(reasons)


def office_processes():
    """只查询 PID/映像，不接触 Office COM/文档。"""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    ids = (wintypes.DWORD * 16384)()
    size = wintypes.DWORD()
    ctypes.WinDLL("psapi").EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(size))
    result = {}
    for pid in ids[:size.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                if Path(buffer.value).name.upper() in OFFICE_NAMES:
                    result[pid] = buffer.value
            kernel.CloseHandle(handle)
    return result


def isolated_worker(config_path, timeout=60):
    """挂起创建 worker，先加入 kill-on-close Job，再允许执行。"""
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    job = win32job.CreateJobObject(None, config["job_name"])
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    command = subprocess.list2cmdline([sys.executable, str(Path(__file__).resolve()), "--worker", str(config_path)])
    process = thread = None
    started = time.monotonic()
    try:
        process, thread, pid, _ = win32process.CreateProcess(
            None, command, None, None, False,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW, None, str(ROOT), win32process.STARTUPINFO())
        win32job.AssignProcessToJobObject(job, process)
        win32process.ResumeThread(thread)
        timed_out = win32event.WaitForSingleObject(process, int(timeout * 1000)) == win32event.WAIT_TIMEOUT
        if timed_out:
            win32job.TerminateJobObject(job, 124)
            win32event.WaitForSingleObject(process, 5000)
        return {"worker_pid": pid, "timed_out": timed_out,
                "exit_code": win32process.GetExitCodeProcess(process),
                "seconds": round(time.monotonic() - started, 3)}
    finally:
        # 包括 COM 阻塞和正常退出后残留的、已加入本任务 Job 的 Office。
        if process:
            if win32process.GetExitCodeProcess(process) == 259:
                win32api.TerminateProcess(process, 125)
            process.Close()
        if thread:
            thread.Close()
        job.Close()


def make_fixtures(directory):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches as WordInches
    from PIL import Image, ImageDraw, ImageFont
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    directory = Path(directory)
    image = Image.new("RGB", (900, 240), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 38)
    draw.rectangle((10, 10, 890, 230), outline="#176d71", width=5)
    draw.text((35, 75), "仅在图中：青苗站 → 山泉站，预算 37 dB", fill="#163638", font=font)
    image.save(directory / "diagram.png")
    doc = Document()
    doc.styles["Normal"].font.name = "Microsoft YaHei"
    doc.styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    doc.add_heading("隔离转换中文样本", 0)
    doc.sections[0].header.paragraphs[0].text = "页眉：无敏感演示资料"
    doc.sections[0].footer.paragraphs[0].text = "页脚：固定文本，不含页码域"
    doc.add_paragraph("正文：链路预算 42 dB。图中另有独有数值。")
    doc.add_picture(str(directory / "diagram.png"), width=WordInches(6))
    equation = OxmlElement("m:oMath")
    run = OxmlElement("m:r")
    text = OxmlElement("m:t")
    text.text = "P = 2 × 3 = 6 W"
    run.append(text)
    equation.append(run)
    doc.add_paragraph()._p.append(equation)
    paragraph = doc.add_paragraph("修订层：")
    for tag, value in (("del", "旧预算 12 dB"), ("ins", "新预算 42 dB")):
        revision = OxmlElement("w:" + tag)
        revision.set(qn("w:id"), "1" if tag == "del" else "2")
        revision.set(qn("w:author"), "隔离样本")
        revision.set(qn("w:date"), "2026-11-12T00:00:00Z")
        r = OxmlElement("w:r")
        t = OxmlElement("w:delText" if tag == "del" else "w:t")
        t.text = value
        r.append(t)
        revision.append(r)
        paragraph._p.append(revision)
    doc.add_comment(doc.add_paragraph("批注锚点").runs[0], text="批注附加层：待复核", author="隔离样本")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "样本编号", "功率 W"
    for i in range(70):
        cells = table.add_row().cells
        cells[0].text, cells[1].text = f"站点 {i + 1:02}", str(i + 10)
    doc.save(directory / "sample.docx")
    ppt = Presentation()
    for index in range(3):
        slide = ppt.slides.add_slide(ppt.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(.5), Inches(.3), Inches(9), Inches(1))
        box.text = f"幻灯片 {index + 1}：" + ("隐藏页预算 59 dB" if index == 1 else "中文图文演示")
        for p in box.text_frame.paragraphs:
            for r in p.runs:
                r.font.name, r.font.size = "Microsoft YaHei", Pt(28)
        slide.shapes.add_picture(str(directory / "diagram.png"), Inches(.5), Inches(1.5), width=Inches(8))
        for left, label in ((.5, "输入 6 W"), (4, "输出 3 W")):
            shape = slide.shapes.add_shape(MSO_SHAPE.CHEVRON, Inches(left), Inches(4), Inches(3), Inches(1))
            shape.text = label
        slide.notes_slide.notes_text_frame.text = f"备注附加层 {index + 1}：校验值 {80 + index}"
        if index == 1:
            slide._element.set("show", "0")
    ppt.save(directory / "sample.pptx")
    return [directory / "sample.docx", directory / "sample.pptx"]


def worker(config):
    import pythoncom
    import pywintypes
    import win32api
    import win32com.client.dynamic
    import win32job
    import win32process

    if config.get("hang"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        dump(config["result"], {"descendant_pid": child.pid})
        time.sleep(300)
        return
    result = {"status": "blocked", "source_sha256": sha(config["source"])}
    app = doc = None
    owned = False
    pythoncom.CoInitialize()
    try:
        if office_processes():
            raise RuntimeError("Office 已运行；不创建或连接 COM")
        source = Path(config["source"])
        reasons = preflight(source)
        if reasons:
            raise RuntimeError("preflight:" + ",".join(reasons))
        created_after = time.time()
        clsid = "Word.Application" if source.suffix == ".docx" else "PowerPoint.Application"
        # 无 GetActiveObject/Dispatch；不读取用户文档。无已运行实例才允许激活。
        raw = pythoncom.CoCreateInstance(pywintypes.IID(clsid), None,
                                         pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch)
        app = win32com.client.dynamic.Dispatch(raw)
        processes = office_processes()
        expected = "WINWORD.EXE" if source.suffix == ".docx" else "POWERPNT.EXE"
        candidates = [p for p, image in processes.items() if Path(image).name.upper() == expected]
        if len(candidates) != 1:
            raise RuntimeError("无法证明 Office 进程身份")
        pid = candidates[0]
        handle = win32api.OpenProcess(0x100 | 0x1000 | 1, False, pid)
        try:
            times = win32process.GetProcessTimes(handle)
            if times["CreationTime"].timestamp() < created_after:
                raise RuntimeError("拒绝已有进程：不退出、不关闭其文档")
            job = win32job.OpenJobObject(0x0001, False, config["job_name"])
            try:
                win32job.AssignProcessToJobObject(job, handle)
            finally:
                job.Close()
            owned = True
        finally:
            handle.Close()
        result.update(office_pid=pid, office_image=processes[pid], version=str(app.Version),
                      isolation="fresh PID + creation time + dedicated kill-on-close Job")
        if config.get("hang_after_ownership"):
            dump(config["result"], result)
            time.sleep(300)
        app.AutomationSecurity = 3
        if int(app.AutomationSecurity) != 3:
            raise RuntimeError("无法确认 AutomationSecurity=ForceDisable")
        result["automation_security"] = int(app.AutomationSecurity)
        if source.suffix == ".docx":
            app.Visible = False
            app.DisplayAlerts = 0
            # 不触碰 Application.Options、注册表、Trust Center、NormalTemplate。
            doc = app.Documents.Open(str(source), False, True, False, "", "", False, "", "", 0, 0, False, False, 0, True)
            doc.ActiveWindow.View.RevisionsView = 0
            doc.ActiveWindow.View.ShowRevisionsAndComments = False
            result["layers"] = {"comments": [str(c.Range.Text) for c in doc.Comments],
                                "revisions": [str(r.Range.Text) for r in doc.Revisions]}
            mappings = []
            for index in range(1, doc.Paragraphs.Count + 1):
                r = doc.Paragraphs(index).Range
                start, end = r.Duplicate, r.Duplicate
                start.Collapse(1)
                end.Collapse(0)
                mappings.append({"paragraph": index, "start": r.Start, "end": r.End,
                                 "reading_pdf_pages": [int(start.Information(3)), int(end.Information(3))],
                                 "text": str(r.Text).strip()[:100]})
            result["mapping"] = mappings
            result["tables"] = int(doc.Tables.Count)
            result["equations"] = int(doc.OMaths.Count)
            result["options"] = {"read_only": bool(doc.ReadOnly), "final_view": True,
                                 "export_item": "document content, without markup", "update_fields": "never called"}
            doc.ExportAsFixedFormat(str(config["pdf"]), 17, False, 0, 0, 1, 1, 0, True, True, 0, True, True, False)
            doc.Close(0)
        else:
            doc = app.Presentations.Open(str(source), -1, 0, 0)
            result["mapping"] = []
            for i in range(1, doc.Slides.Count + 1):
                slide = doc.Slides(i)
                notes = []
                for shape in slide.NotesPage.Shapes:
                    if shape.HasTextFrame and shape.TextFrame.HasText:
                        notes.append(str(shape.TextFrame.TextRange.Text))
                result["mapping"].append({"slide": i, "slide_id": int(slide.SlideID), "reading_pdf_page": i,
                                          "hidden": bool(slide.SlideShowTransition.Hidden), "notes": notes,
                                          "objects": [{"id": int(s.Id), "name": str(s.Name)} for s in slide.Shapes]})
            result["options"] = {"read_only": True, "include_hidden_slides": True, "output": "slides"}
            # late-bound 调用需要完整位置参数；PrintHiddenSlides=-1。
            doc.ExportAsFixedFormat(str(config["pdf"]), 2, 2, 0, 1, 1, -1, None, 1, "", True, True, True, True, False)
            doc.Close()
        doc = None
        result["status"] = "converted"
        result["pdf_sha256"] = sha(config["pdf"])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if owned:
            try:
                if doc is not None:
                    doc.Close(0) if Path(config["source"]).suffix == ".docx" else doc.Close()
                app.Quit(0) if Path(config["source"]).suffix == ".docx" else app.Quit()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        doc = app = None
        pythoncom.CoUninitialize()
        result["source_unchanged"] = sha(config["source"]) == result["source_sha256"]
        dump(config["result"], result)


def execute(directory, timeout):
    directory = Path(directory).resolve()
    directory.relative_to(BASE.resolve())
    directory.mkdir(parents=True, exist_ok=False)
    before = office_processes()
    if before:
        dump(directory / "report.json", {"status": "blocked", "reason": "Office 正在运行，不连接或关闭", "pids": list(before)})
        return
    report = {"status": "blocked", "production_enabled": False, "python": sys.executable,
              "dependencies": {x: importlib.util.find_spec(x) is not None for x in ("win32com", "docx", "pptx", "fitz")},
              "before_office_pids": list(before), "formats": {}}
    for source in sorted((BASE / "fixtures").glob("sample.*")):
        if source.suffix not in {".docx", ".pptx"}:
            continue
        copy = directory / source.name
        shutil.copyfile(source, copy)
        config = {"job_name": "CowAgentOfficeProbe-" + uuid.uuid4().hex,
                  "source": str(copy), "result": str(directory / (source.suffix[1:] + ".json")),
                  "pdf": str(directory / (source.suffix[1:] + ".pdf"))}
        config_path = directory / (source.suffix[1:] + "-worker.json")
        dump(config_path, config)
        run = isolated_worker(config_path, timeout)
        if Path(config["result"]).exists():
            run["evidence"] = json.loads(Path(config["result"]).read_text(encoding="utf-8"))
            if Path(config["pdf"]).exists():
                check_pdf(config["pdf"], run["evidence"])
                dump(config["result"], run["evidence"])
        run["original_unchanged"] = sha(source) == sha(copy)
        report["formats"][source.suffix] = run
    for _ in range(40):
        remaining = office_processes()
        if not remaining:
            break
        time.sleep(.05)
    report["after_office_pids"] = list(remaining)
    report["blockers"] = ["项目 .venv 未具备 win32com；未验证打包环境",
                          "活动字段一律拒绝：未证明保留已有域显示值、脚注尾注及所有动态内容的安全转换",
                          "未隔离 Office 启动模板/COM 加载项；无任意用户文档安全保证",
                          "字体替换、旧格式、锁定损坏输入及动画/媒体等未完成验收"]
    dump(directory / "report.json", report)
    print(json.dumps({"status": report["status"], "report": str(directory / "report.json"),
                      "formats": {k: v.get("evidence", {}).get("status", "unknown")
                                  for k, v in report["formats"].items()}}, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.worker:
        worker(json.loads(args.worker.read_text(encoding="utf-8")))
    elif args.fixtures:
        directory = BASE / "fixtures"
        directory.mkdir(parents=True, exist_ok=True)
        print([str(p) for p in make_fixtures(directory)])
    elif args.run:
        execute(args.directory or BASE / time.strftime("run-%Y%m%d-%H%M%S"), args.timeout)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
