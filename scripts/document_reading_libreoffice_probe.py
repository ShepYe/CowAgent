"""本地样本验证：不访问模型 API，不登记知识，不启动可见 Office 窗口。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.knowledge.office_materials import OfficeMaterialError, prepare_office_materials
from pypdf import PdfReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "tmp/document-reading-office")
    parser.add_argument("--soffice", default="soffice")
    parser.add_argument("--source", action="append", help="根内相对 DOCX/PPTX 路径，可重复")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    results = []
    for relative in args.source or ["fixtures/sample.docx", "fixtures/sample.pptx"]:
        start = time.monotonic()
        record = dict(source=relative)
        before = None
        try:
            before = sha256((args.root / relative).read_bytes()).hexdigest()
            result = prepare_office_materials(args.root, relative, soffice=args.soffice,
                                              timeout_seconds=args.timeout)
            pdf = PdfReader(args.root / result.material_relpath)
            first = result.to_dict()
            second = prepare_office_materials(args.root, relative, soffice=args.soffice,
                                              timeout_seconds=args.timeout)
            record.update(status="converted", materials=first, cache_reused=second.to_dict() == first,
                          text_pages=[page.extract_text() or "" for page in pdf.pages],
                          visual_quality="未目视验收；转换成功不等于排版或图表保真已通过")
        except OfficeMaterialError as exc:
            record.update(status="failed", code=exc.code, message=str(exc))
        except OSError as exc:
            record.update(status="failed", code="io_error", message=str(exc))
        if before is not None:
            record["original_hash_unchanged"] = before == sha256((args.root / relative).read_bytes()).hexdigest()
        record["elapsed_seconds"] = round(time.monotonic() - start, 3)
        results.append(record)
    report = dict(results=results, cloud_api_called=False)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output)
    return 0 if all(item["status"] == "converted" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
