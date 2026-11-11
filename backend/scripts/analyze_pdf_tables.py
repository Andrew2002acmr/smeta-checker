"""Prepare original PDF pages without LLM calls; export evidence and Docling JSON."""
import argparse
import json
from pathlib import Path

from pdf_table_export import prepare_pages, save_exports, source_identity




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pages", type=int, nargs="+", help="Original one-based PDF page numbers; default: all")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(".audit_cache/table-extraction"))
    args = parser.parse_args()
    source = source_identity(args.pdf)
    records = []
    for record in prepare_pages(args.pdf, args.pages, cache_dir=args.cache_dir):
        records.append(record)
        print(json.dumps(dict(page=record["page"], errors=record["errors"], tables=[dict(
            table_id=t["table_id"], rows=t["row_count"], columns=t["col_count"],
            cells=len(t["cells"]), numeric_verified=sum(bool(c.get("numeric_verified")) for c in t["cells"]),
            consistency=t.get("consistency", {}).get("state")) for t in record["tables"]]), ensure_ascii=False), flush=True)
    values = save_exports(records, source, args.out)
    print(json.dumps(dict(pdf_sha256=source["sha256"], pages=[r["page"] for r in records],
        exports=list(values), retrieval_documents=len(values["openwebui.json"])), ensure_ascii=False), flush=True)
    if any(record["errors"] for record in records):
        raise SystemExit("Preparation has errors; exported evidence is partial, not verified.")


if __name__ == "__main__":
    main()
