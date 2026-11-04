"""Read-only targeted native-resource candidate export for an inactive KB build.

This is the narrow entry point for the relocated existing continuation parser.
No active pointer, database row, model provider or external build tree is changed.
After independent visual review, a staging transaction can use the parser's
existing apply_reviewed API. Keep the previous continuation metadata alongside
that new staging proof; this exporter never supplies an automatic visual review.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pymupdf

from scripts import normative_resource_continuation as parser


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def targeted_candidates(
    db, root, source_id, header_page, page_number, *, norm_codes=(), resource_codes=()
) -> dict[str, Any]:
    """Select only by literal identities; values never select a column or row."""
    root = Path(root).resolve()
    if (
        type(header_page) is not int
        or type(page_number) is not int
        or page_number != header_page + 1
    ):
        raise ValueError("Target pages must be adjacent positive integers")
    source = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        raise ValueError("Unknown source")
    source = dict(source)
    path = (root / source["blob_path"]).resolve()
    if not path.is_relative_to(root) or file_sha(path) != source["sha256"]:
        raise ValueError("Unverified source blob")
    norm_ids = {
        row["code"]: row["id"]
        for row in db.execute(
            "SELECT id,code FROM norms WHERE source_id=?", (source_id,)
        )
    }
    existing = {
        (row["norm_id"], row["resource_code"])
        for row in db.execute(
            "SELECT r.norm_id,r.resource_code FROM resources r JOIN norms n ON n.id=r.norm_id WHERE n.source_id=?",
            (source_id,),
        )
    }
    with pymupdf.open(path) as pdf:
        if not 1 <= header_page < page_number <= len(pdf):
            raise ValueError("Target pages outside PDF")
        before, page = pdf[header_page - 1], pdf[page_number - 1]
        before_native, native = parser.Native(before), parser.Native(page)
        headers = sorted(
            parser.grids(before, before_native), key=lambda t: t["bbox"][1]
        )
        tables = sorted(parser.grids(page, native), key=lambda t: t["bbox"][1])
        if not headers or not tables:
            raise ValueError("Missing native resource grid")
        previous = parser.context_for(headers[-1], header_page, before.rect.height)
        reason = parser.continuation_reason(
            previous, tables[0], page, page_number, native
        )
        if reason:
            raise ValueError("Unsafe continuation: " + reason)
        rows, rejected = parser.extract_proposal(
            source, previous, tables[0], native, page_number, norm_ids, existing
        )
    chosen = [
        row
        for row in rows
        if (not norm_codes or row["proof"]["norm_code"] in norm_codes)
        and (not resource_codes or row["resource_code"] in resource_codes)
    ]
    identities = [(row["norm_id"], row["resource_code"]) for row in chosen]
    if len(identities) != len(set(identities)):
        raise ValueError("Ambiguous duplicate target resource identity")
    return dict(
        status="native_candidate_requires_visual_review",
        rows=chosen,
        rejected=rejected,
        source_sha256=source["sha256"],
        header_page=header_page,
        physical_page=page_number,
        selection=dict(
            norm_codes=list(norm_codes),
            resource_codes=list(resource_codes),
            selected_by_quantity=False,
        ),
        parser_version=parser.VERSION,
        parser_sha256=file_sha(Path(parser.__file__)),
        upstream=parser.UPSTREAM,
        model_calls=0,
        ocr_calls=0,
        automatic_application_allowed=False,
        database_writes=0,
    )


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--root", type=Path, required=True)
    args.add_argument("--database-relative", required=True)
    args.add_argument("--database-sha256", required=True)
    args.add_argument("--source-id", required=True)
    args.add_argument("--header-page", type=int, required=True)
    args.add_argument("--page", type=int, required=True)
    args.add_argument("--norm-code", action="append", default=[])
    args.add_argument("--resource-code", action="append", default=[])
    args.add_argument("--output-json", type=Path, required=True)
    options = args.parse_args(argv)
    root = options.root.resolve()
    database = (root / options.database_relative).resolve()
    if (
        not database.is_relative_to(root)
        or file_sha(database) != options.database_sha256
    ):
        raise ValueError("Pinned database mismatch")
    output = options.output_json.resolve()
    if output.is_relative_to(root):
        raise ValueError("Diagnostic output must be outside the knowledge root")
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        result = targeted_candidates(
            db,
            root,
            options.source_id,
            options.header_page,
            options.page,
            norm_codes=options.norm_code,
            resource_codes=options.resource_code,
        )
    if file_sha(database) != options.database_sha256:
        raise ValueError("Pinned database changed during read-only export")
    result["database_pin"] = dict(
        database=options.database_relative, sha256=options.database_sha256
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            dict(output=str(output), rows=len(result["rows"]), database_writes=0)
        )
    )


if __name__ == "__main__":
    main()
