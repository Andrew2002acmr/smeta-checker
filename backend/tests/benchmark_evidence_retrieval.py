"""Offline evidence-retrieval benchmark: prepare snapshots, compare channels.

Commands (from the repository root)::

    uv run python tests/benchmark_evidence_retrieval.py prepare \\
        --gold docs/research/2026-10-19-pd-vor/gold.json \\
        --output .audit_cache/evidence-retrieval/corpus
    uv run python tests/benchmark_evidence_retrieval.py compare \\
        --gold docs/research/2026-10-19-pd-vor/gold.json \\
        --corpus .audit_cache/evidence-retrieval/corpus \\
        --output .audit_cache/evidence-retrieval/comparison

``prepare`` performs the single real local extraction/OCR pass in fixed
position order. ``compare`` is strictly offline: it loads frozen snapshots
via ``PageTextIndex.from_snapshot`` and never touches PDF, OCR, network, or
providers. Gold manual transcriptions are expectations only and are never
injected as index text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

CHANNELS = ("current", "exact", "bm25", "rrf")


def _gold_paths(gold_path: Path) -> tuple[Path, Path]:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    package = gold_path.parent
    sources = gold.get("sources", {})
    pdf_rel = sources.get("pd", {}).get("package_path", "sources/pd.pdf")
    vor_rel = sources.get("vor", {}).get("package_path", "sources/vor.xlsx")
    pdf_path = package / pdf_rel if not Path(pdf_rel).is_absolute() else Path(pdf_rel)
    vor_path = package / vor_rel if not Path(vor_rel).is_absolute() else Path(vor_rel)
    for candidate in (package / "sources" / "pd.pdf", package / "sources" / "vor.xlsx"):
        if candidate.exists() and candidate.stat().st_size > 0:
            if "pd.pdf" in candidate.name and not pdf_path.exists():
                pdf_path = candidate
            if "vor.xlsx" in candidate.name and not vor_path.exists():
                vor_path = candidate
    return pdf_path, vor_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_info() -> dict:
    import pymupdf  # noqa: PLC0415  (local import keeps CLI import cheap)

    try:
        from importlib.metadata import version as pkg_version
    except Exception:  # pragma: no cover - stdlib always has it
        pkg_version = None
    info: dict[str, str | None] = {"python": sys.version.split()[0]}
    info["pymupdf"] = getattr(pymupdf, "VersionBind", None)
    for name, key in (("pymupdf4llm", "pymupdf4llm"),
                      ("pymupdf-layout", "pymupdf-layout"),
                      ("tesseract", "tesseract")):
        try:
            info[key] = pkg_version(name) if pkg_version else None
        except Exception:
            info[key] = None
    try:
        import shutil
        import subprocess

        proc = subprocess.run(
            [shutil.which("tesseract") or "tesseract", "--version"],
            capture_output=True, text=True, timeout=10)
        info["tesseract_version"] = (proc.stdout or proc.stderr or "").splitlines()[0][:120]
    except Exception:
        info["tesseract_version"] = None
    return info


def _maybe_reuse_corpus(out_dir: Path, gold_path: Path, pdf_path: Path,
                        vor_path: Path) -> None:
    """Verify a compatible existing corpus and reuse it instead of re-extracting.

    Cold/warm identity proof: every stored snapshot must still verify via
    ``PageTextIndex.from_snapshot`` and match its manifest ``corpus_id``.
    A compatible manifest (same gold/pdf/vor SHA, snapshot schema, and
    extraction settings) means the existing corpus IS the run output and
    ``prepare`` exits 0 without touching PDF/OCR. Anything incompatible is
    an error, never a silent overwrite.
    """
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        if any(out_dir.iterdir()):
            raise SystemExit(
                "output directory holds files without a manifest: %s; "
                "refusing silent reuse" % out_dir)
        return
    from audit_core import PageTextIndex  # noqa: PLC0415

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for key, path in (("gold_sha256", gold_path), ("pdf_sha256", pdf_path),
                      ("vor_sha256", vor_path)):
        try:
            if _sha256_file(path) != manifest.get(key):
                problems.append(key)
        except OSError:
            problems.append(key)
    if manifest.get("snapshot_schema") != 1:
        problems.append("snapshot_schema")
    expected_settings = {"language": "rus+eng", "dpi": 200,
                         "max_pixels": 12000000, "max_side": 4096,
                         "max_ocr_pages_per_position": 4,
                         "table_pages_per_position": 8}
    if (manifest.get("extraction") or {}).get("settings") != expected_settings:
        problems.append("extraction_settings")
    for entry in manifest.get("positions", []):
        snapshot_path = out_dir / entry.get("file", "")
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except OSError:
            problems.append("missing:%s" % entry.get("file"))
            break
        if snapshot.get("corpus_id") != entry.get("corpus_id"):
            problems.append("corpus_id:%s" % entry.get("file"))
            break
        try:
            if PageTextIndex.from_snapshot(snapshot).to_snapshot() != snapshot:
                problems.append("roundtrip:%s" % entry.get("file"))
                break
        except ValueError:
            problems.append("verify:%s" % entry.get("file"))
            break
    if problems:
        raise SystemExit(
            "incompatible existing corpus (%s); refusing silent overwrite: %s"
            % (", ".join(sorted(set(problems))), out_dir))
    print("reusing compatible corpus: %d positions (%s)"
          % (len(manifest.get("positions", [])),
             manifest.get("pdf_sha256", "")[:12]))
    raise SystemExit(0)


def cmd_prepare(args: argparse.Namespace) -> int:
    from audit_core import PageTextIndex, parse_page_reference  # noqa: PLC0415
    from audit_models import expected_scope_hint  # noqa: PLC0415
    from pdf_text import PDFTextSource  # noqa: PLC0415
    from vor_parse import load_vor_frame_positions  # noqa: PLC0415
    import pymupdf  # noqa: PLC0415

    gold_path = Path(args.gold)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path, vor_path = _gold_paths(gold_path)
    if not pdf_path.exists():
        raise SystemExit("PDF source missing: %s" % pdf_path)
    if not vor_path.exists():
        raise SystemExit("VOR source missing: %s" % vor_path)

    _maybe_reuse_corpus(out_dir, gold_path, pdf_path, vor_path)

    positions_source = gold.get("positions", [])
    df, positions, _meta = load_vor_frame_positions(
        str(vor_path), vor_path.name)
    _ = df

    started = time.monotonic()
    wall_cpu = time.process_time()
    ocr_calls = 0
    corpus_entries: list[dict] = []
    position_reports: list[dict] = []
    max_rss_kb = 0
    try:
        import resource

        def _rss() -> int:
            try:
                return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            except Exception:
                return 0
    except ImportError:  # pragma: no cover - non-POSIX
        def _rss() -> int:
            return 0

    doc = pymupdf.open(pdf_path)
    try:
        for number, entry in enumerate(positions_source, start=1):
            excel_row = int(entry.get("excel_row"))
            position = next(
                (p for p in positions
                 if p.position_source is not None
                 and int(p.position_source.row) == excel_row),
                None,
            )
            if position is None:
                raise SystemExit(
                    "VOR row %d (position %d) not parsed; refusing to reorder" %
                    (excel_row, number))
            if int(entry.get("number")) != int(position.number or 0):
                raise SystemExit(
                    "fixed-order mismatch at gold #%d: VOR row %d parsed as %r" %
                    (number, excel_row, position.number))
            scope = str(entry.get("scope") or "")
            target_indices, _, _ = parse_page_reference(
                position.page_ref, len(doc))
            source = PDFTextSource(doc, cache_dir=str(out_dir / "pdf-cache"))
            try:
                before_ocr = source.ocr_count
                index = source.for_position(
                    target_indices,
                    position.work_name, position.volume, position.reference,
                    position.comment,
                    expected_scope=expected_scope_hint(position.position_source),
                )
                ocr_calls += source.ocr_count - before_ocr
                # Metadata must enter before hashing: corpus_id covers it.
                index.metadata.update({
                    "position_number": number,
                    "scope": scope,
                    "expected_scope": expected_scope_hint(position.position_source),
                    "vor_row": excel_row,
                    "work_name": position.work_name,
                    "volume": position.volume,
                    "unit": position.unit,
                    "page_ref": position.page_ref,
                })
                snapshot = index.to_snapshot()
                # Re-verify: the stored snapshot must round-trip byte-identical.
                restored = PageTextIndex.from_snapshot(snapshot)
                if restored.to_snapshot() != snapshot:
                    raise SystemExit(
                        "snapshot round-trip mismatch at position %d" % number)
                name = "position-%02d.json" % number
                (out_dir / name).write_text(
                    json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
                corpus_entries.append({"number": number, "file": name,
                                       "corpus_id": snapshot["corpus_id"]})
                position_reports.append({
                    "number": number, "scope": scope,
                    "vor_row": excel_row,
                    "corpus_id": snapshot["corpus_id"],
                    "snapshot_bytes": len(json.dumps(
                        snapshot, ensure_ascii=False).encode("utf-8")),
                    "errors": {str(k): v for k, v in
                               dict(snapshot.get("errors") or {}).items()},
                    "pending_ocr_1based": [
                        i + 1 for i in snapshot.get("pending_ocr") or []],
                })
            finally:
                source.close()
            max_rss_kb = max(max_rss_kb, _rss())
    finally:
        doc.close()

    manifest = {
        "schema": 1,
        "gold_path": str(gold_path),
        "gold_sha256": _sha256_file(gold_path),
        "pdf_sha256": _sha256_file(pdf_path),
        "vor_sha256": _sha256_file(vor_path),
        "snapshot_schema": 1,
        "positions": corpus_entries,
        "extraction": {
            "versions": _version_info(),
            "settings": {"language": "rus+eng", "dpi": 200,
                         "max_pixels": 12000000, "max_side": 4096,
                         "max_ocr_pages_per_position": 4,
                         "table_pages_per_position": 8},
            "cache_schema": 2,
        },
        "resource": {
            "wall_seconds": round(time.monotonic() - started, 3),
            "cpu_seconds": round(time.process_time() - wall_cpu, 3),
            "max_rss_kb": max_rss_kb,
            "ocr_new_passes": ocr_calls,
            "pdf_bytes": pdf_path.stat().st_size,
            "snapshot_bytes": sum(p["snapshot_bytes"]
                                  for p in position_reports),
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "positions.json").write_text(
        json.dumps(position_reports, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("prepared %d positions in %ss (%d new OCR passes)" % (
        len(corpus_entries), manifest["resource"]["wall_seconds"], ocr_calls))
    return 0


def _other_scopes(gold_positions: list[dict], scope: str) -> list[str]:
    return sorted({str(p.get("scope") or "") for p in gold_positions
                   if str(p.get("scope") or "") and
                   str(p.get("scope") or "") != scope})


def _channel_rankings(index, position, gold_entry) -> dict[str, list[int]]:
    from evidence_retrieval import (  # noqa: PLC0415
        bm25_rank, exact_rank, rrf_fuse, token_stream,
    )

    query = "%s %s %s %s" % (
        position.work_name, position.comment, position.reference,
        position.volume)
    texts = list(index.texts)
    current = index.candidates(
        position.work_name, position.comment, position.reference,
        position.volume)
    exact, _details = exact_rank(
        texts, position.work_name, position.comment,
        position.volume, position.unit)
    page_streams = [token_stream(text) for text in texts]
    bm25 = bm25_rank(page_streams, query)
    fused = rrf_fuse([current, exact, bm25])
    scope = str(gold_entry.get("scope") or "")
    return {"scope": scope, "current": current, "exact": exact,
            "bm25": bm25, "rrf": fused}


def cmd_compare(args: argparse.Namespace) -> int:
    import tracemalloc  # noqa: PLC0415

    from audit_core import PageTextIndex, parse_page_reference  # noqa: PLC0415
    from audit_models import expected_scope_hint  # noqa: PLC0415
    from evidence_retrieval import (  # noqa: PLC0415
        MAX_CONTEXT_CHARS, MAX_PAGES, apply_scope_filter, assemble_context,
        evaluate_chain, row_header_binding,
    )
    from vor_parse import load_vor_frame_positions  # noqa: PLC0415

    gold_path = Path(args.gold)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    corpus_dir = Path(args.corpus)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()) and not (out_dir / "comparison.json").exists():
        raise SystemExit(
            "output directory holds unrelated files without comparison.json: "
            "%s; refusing silent reuse" % out_dir)

    manifest = json.loads((corpus_dir / "manifest.json").read_text(
        encoding="utf-8"))
    pdf_path, vor_path = _gold_paths(gold_path)
    for key, path in (("pdf_sha256", pdf_path), ("vor_sha256", vor_path)):
        if _sha256_file(path) != manifest.get(key):
            raise SystemExit(
                "incompatible corpus: %s changed; refusing silent reuse (%s)" %
                (path, key))
    if _sha256_file(gold_path) != manifest.get("gold_sha256"):
        raise SystemExit("incompatible corpus: gold.json changed")
    _ = parse_page_reference, apply_scope_filter

    _, positions, _ = load_vor_frame_positions(str(vor_path), vor_path.name)
    gold_positions = gold.get("positions", [])
    gold_evidence = gold.get("evidence", {})
    expected_ids = sorted({eid for p in gold_positions
                           for eid in p.get("evidence_ids", [])})
    missing = [eid for eid in expected_ids if eid not in gold_evidence]
    if missing:
        raise SystemExit("gold evidence missing: %s" % ", ".join(missing[:8]))

    tracemalloc.start()
    started = time.monotonic()
    cpu_started = time.process_time()
    method_totals = {name: {"recall_hits": 0, "contexts": 0, "chars": 0}
                     for name in CHANNELS}
    all_positions: list[dict] = []
    delivery_rows: list[dict] = []
    false_confirmed = {name: [] for name in CHANNELS}
    per_class: dict[str, dict[str, dict[str, int]]] = {}
    for number, entry in enumerate(gold_positions, start=1):
        excel_row = int(entry.get("excel_row"))
        position = next(
            (p for p in positions if p.position_source is not None and
             int(p.position_source.row) == excel_row), None)
        if position is None:
            raise SystemExit("VOR row %d missing in compare" % excel_row)
        snapshot = json.loads(
            (corpus_dir / ("position-%02d.json" % number)).read_text(
                encoding="utf-8"))
        entry_manifest = next(
            (e for e in manifest.get("positions", [])
             if e.get("number") == number), {})
        if snapshot.get("corpus_id") != entry_manifest.get("corpus_id"):
            raise SystemExit(
                "corpus mismatch at position %d: refusing silent reuse" % number)
        index = PageTextIndex.from_snapshot(snapshot)  # frozen; no PDF/OCR
        expected_scope = expected_scope_hint(position.position_source) or ""
        others = _other_scopes(gold_positions, str(entry.get("scope") or ""))
        primary, _, primary_1based = parse_page_reference(
            position.page_ref, len(index.texts))
        rankings = _channel_rankings(index, position, entry)
        pos_report: dict = {
            "number": number, "scope": entry.get("scope"),
            "vor_row": excel_row, "corpus_id": snapshot.get("corpus_id"),
            "primary_1based": primary_1based,
            "work_name": position.work_name, "volume": position.volume,
            "unit": position.unit,
            "expected_evidence": list(entry.get("evidence_ids", [])),
            "channels": {},
        }
        for name in CHANNELS:
            ranked = [i for i in rankings[name] if 0 <= i < len(index.texts)]
            assembly = assemble_context(
                index, ranked, primary, position.work_name, position.comment,
                position.reference, position.volume, position.formula or "",
                expected_scope, others)
            if len(assembly["ordered_1based"]) > MAX_PAGES:
                raise SystemExit("budget breach: pages")
            if assembly["context_chars"] > MAX_CONTEXT_CHARS:
                raise SystemExit("budget breach: chars")
            chains = []
            for eid in entry.get("evidence_ids", []):
                chain = evaluate_chain(
                    eid, gold_evidence[eid], index, ranked, assembly,
                    position.volume, position.unit)
                chain["channel"] = name
                chain["position"] = number
                chains.append(chain)
                delivery_rows.append({**chain,
                                      "expected_scope": expected_scope})
            classes = _evidence_classes(entry, gold_evidence)
            for evidence_class in classes:
                bucket = per_class.setdefault(
                    evidence_class, {n: {"n": 0, "hit": 0} for n in CHANNELS})
                bucket[name]["n"] += sum(
                    1 for c in chains
                    if c["evidence_id"] in classes[evidence_class]
                    and not str(gold_evidence[c["evidence_id"]].get("type", "")
                               ).startswith("visual_"))
                bucket[name]["hit"] += sum(
                    1 for c in chains
                    if c["evidence_id"] in classes[evidence_class]
                    and c.get("sufficient"))
            method_totals[name]["contexts"] += 1
            method_totals[name]["chars"] += assembly["context_chars"]
            shipped = [c for c in chains if c.get("sufficient")]
            negatives = _negative_probes(
                index, assembly, position, expected_scope, others)
            for probe in negatives:
                if probe.get("sufficient"):
                    false_confirmed[name].append({
                        "position": number,
                        "evidence_id": "NEGATIVE:" + probe.get("name", "?"),
                        "probe": probe})
            contradictory = [c for c in chains
                             if c.get("loss") == "source_conflict"]
            for chain in contradictory:
                false_confirmed[name].append(
                    {"position": number, "evidence_id": chain["evidence_id"]})
            pos_report["channels"][name] = {
                "ranking_1based": [i + 1 for i in ranked],
                "ordered_1based": assembly["ordered_1based"],
                "first_1based": assembly["first_1based"],
                "context_pages_1based": sorted(assembly["context"]),
                "context_chars": assembly["context_chars"],
                "scope_dropped_lines": assembly["scope_dropped_lines"],
                "scope_ambiguous": assembly["scope_ambiguous"],
                "sufficient": [c["evidence_id"] for c in shipped],
                "losses": {c["evidence_id"]: c.get("loss") for c in chains},
                "chains": chains,
            }
        # Row/header binding coverage comes from the real grid, once per page.
        pos_report["row_header_binding"] = {
            eid: row_header_binding(
                index, int(gold_evidence[eid].get("page") or 0),
                dict(gold_evidence[eid].get("fields") or {}))
            for eid in entry.get("evidence_ids", [])
            if str(gold_evidence[eid].get("type")) == "table_cells"
        }
        all_positions.append(pos_report)

    snapshot_bytes = sum(
        (corpus_dir / ("position-%02d.json" % p["number"])).stat().st_size
        for p in all_positions)
    _, peak_bytes = tracemalloc.get_traced_memory()
    comparison = {
        "schema": 1,
        "gold_sha256": manifest.get("gold_sha256"),
        "pdf_sha256": manifest.get("pdf_sha256"),
        "vor_sha256": manifest.get("vor_sha256"),
        "snapshot_schema": manifest.get("snapshot_schema"),
        "budgets": {"max_pages": MAX_PAGES,
                    "max_context_chars": MAX_CONTEXT_CHARS,
                    "max_contexts": 6},
        "methods": list(CHANNELS),
        "positions": all_positions,
        "evidence_classes": per_class,
        "expected_evidence_total": sum(
            len(p.get("expected_evidence", [])) for p in all_positions),
        "resource": {
            "wall_seconds": round(time.monotonic() - started, 3),
            "cpu_seconds": round(time.process_time() - cpu_started, 3),
            "peak_rss_bytes": peak_bytes,
            "snapshot_bytes": snapshot_bytes,
        },
    }
    metrics = _summarize(all_positions, gold_evidence, false_confirmed,
                         method_totals, CHANNELS)
    comparison["metrics"] = metrics
    comparison["policy"] = _policy_decision(metrics)
    comparison["policy"]["per_position_regressions"] = {
        name: _per_position_no_regression(all_positions, "current", name)
        for name in CHANNELS if name != "current"}
    for name, regressions in comparison["policy"][
            "per_position_regressions"].items():
        if regressions and comparison["policy"]["default"] == name:
            comparison["policy"] = {
                "default": "current",
                "reason": "per_position_regression_in_" + name,
                "per_position_regressions": comparison["policy"][
                    "per_position_regressions"]}
    (out_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False), encoding="utf-8")
    (out_dir / "comparison.md").write_text(
        _render_markdown(comparison), encoding="utf-8")
    with (out_dir / "delivery.jsonl").open("w", encoding="utf-8") as handle:
        for row in delivery_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("compared %d positions x %d methods -> %s" % (
        len(all_positions), len(CHANNELS), out_dir))
    return 0


def _negative_probes(index, assembly, position, expected_scope,
                       other_scopes: list[str]) -> list[dict]:
    """Controlled adversarial proposals; none may ever confirm.

    Built only from the position's own real data: the expected volume with
    the wrong group scope, the expected number with an incompatible unit,
    and a genuinely different number. Any `sufficient` outcome is a false
    confirmation and blocks promotion.
    """
    from evidence_retrieval import evaluate_chain  # noqa: PLC0415

    probes: list[dict] = []
    wrong_scope = (other_scopes[0] if other_scopes else
                   ("ЛОС" if expected_scope != "ЛОС" else "КНС"))
    texts = list(getattr(index, "texts", ()))
    for name, volume, unit in (
            ("wrong_group_quantity", position.volume, position.unit),
            ("wrong_unit", position.volume,
             "шт." if position.unit != "шт." else "м3"),
            ("wrong_number", "999999", position.unit)):
        pages = [i for i, text in enumerate(texts) if text.strip()]
        if not pages:
            continue
        fake = {"id": "NEGATIVE-" + name, "page": pages[0] + 1,
                "type": "native_quote",
                "quote": texts[pages[0]][:600] or "пусто"}
        chain = evaluate_chain(
            "NEGATIVE-" + name, fake, index, pages[:1], assembly,
            volume, unit)
        chain["name"] = name
        chain["wrong_scope"] = wrong_scope if name == "wrong_group_quantity" else ""
        probes.append(chain)
    return probes


def _evidence_classes(entry: dict, gold_evidence: dict) -> dict[str, list[str]]:
    classes: dict[str, list[str]] = {
        "quantity": [], "work": [], "material": [], "geometry": []}
    for eid in entry.get("evidence_ids", []):
        evidence = gold_evidence.get(eid, {})
        kind = str(evidence.get("type") or "")
        text = (" ".join(str(v) for v in
                         (evidence.get("fields") or {}).values())
                + " " + str(evidence.get("quote") or "")).lower()
        if kind == "table_cells" or any(
                token in text for token in ("м2", "м²", "шт", "м3", "м³")):
            classes["quantity"].append(eid)
        elif "геотекстиль" in text or "дорнит" in text or "т-500" in text:
            classes["material"].append(eid)
        elif "толщин" in text or "площад" in text:
            classes["geometry"].append(eid)
        else:
            classes["work"].append(eid)
    return {key: value for key, value in classes.items() if value}


def _summarize(positions: list[dict], gold_evidence: dict,
               false_confirmed: dict, totals: dict,
               channels: tuple[str, ...]) -> dict:
    metrics: dict = {"channels": {}, "evidence_total": 0}
    evidence_total = sum(len(p.get("expected_evidence", [])) for p in positions)
    metrics["evidence_total"] = evidence_total
    for name in channels:
        sufficient = sum(len(p["channels"][name]["sufficient"]) for p in positions)
        recalled = sum(
            1 for p in positions
            for eid in p.get("expected_evidence", [])
            if any(c["evidence_id"] == eid and c.get("sufficient")
                   for c in p["channels"][name]["chains"]))
        in_first = sum(
            1 for p in positions
            for c in p["channels"][name]["chains"]
            if c.get("in_first_context") and c.get("sufficient"))
        in_full = sum(
            1 for p in positions
            for c in p["channels"][name]["chains"]
            if c.get("in_full_context") and c.get("sufficient"))
        recall_positions = sum(
            1 for p in positions
            if any(c.get("sufficient")
                   for c in p["channels"][name]["chains"]))
        extracted = sum(
            1 for p in positions
            for c in p["channels"][name]["chains"] if c.get("extracted"))
        usable = sum(
            1 for p in positions
            for c in p["channels"][name]["chains"] if c.get("usable"))
        losses: dict[str, int] = {}
        for p in positions:
            for c in p["channels"][name]["chains"]:
                if c.get("loss"):
                    losses[str(c["loss"])] = losses.get(
                        str(c["loss"]), 0) + 1
        metrics["channels"][name] = {
            "sufficient_evidence": [sufficient, evidence_total],
            "evidence_recalled": [recalled, evidence_total],
            "in_first_context": [in_first, evidence_total],
            "in_full_context": [in_full, evidence_total],
            "positions_with_evidence": [recall_positions, len(positions)],
            "extraction_coverage": [extracted, evidence_total],
            "usable_coverage": [usable, evidence_total],
            "false_confirmed_contradictions": len(false_confirmed[name]),
            "losses": losses,
            "contexts": totals[name]["contexts"],
            "context_chars": totals[name]["chars"],
        }
    return metrics


def _policy_decision(metrics: dict) -> dict:
    channels = metrics["channels"]
    base = channels.get("current", {})
    decision = {"default": "current", "reason": "parity_or_no_gain"}
    for name in ("exact", "bm25", "rrf"):
        candidate = channels.get(name, {})
        if not candidate:
            continue
        if candidate.get("false_confirmed_contradictions", 1) != 0:
            continue
        if base.get("false_confirmed_contradictions", 0) != 0:
            continue
        base_hits = (base.get("sufficient_evidence") or [0, 0])[0]
        cand_hits = (candidate.get("sufficient_evidence") or [0, 0])[0]
        if cand_hits <= base_hits:
            continue
        # Per-position gate: the candidate must lose no sufficient evidence
        # on any single position (checked by caller-supplied table).
        decision = {"default": name,
                    "reason": "strict_no_regressions_with_gain"}
    return decision


def _per_position_no_regression(all_positions, base_name="current",
                                candidate_name=""):
    """Sufficient-evidence set of candidate must cover base on every position."""
    regressions = []
    for position in all_positions:
        base_set = set(position["channels"][base_name]["sufficient"])
        cand_set = set(position["channels"][candidate_name]["sufficient"])
        lost = sorted(base_set - cand_set)
        if lost:
            regressions.append({"position": position["number"], "lost": lost})
    return regressions


def _render_markdown(comparison: dict) -> str:
    lines = ["# Evidence retrieval comparison", "",
             "Frozen corpus: snapshots verified by corpus_id; "
             "compare performed no PDF/OCR/network calls.", ""]
    lines.append("## Policy")
    lines.append("")
    lines.append("Default production channel stays `%s` (%s)." % (
        comparison["policy"]["default"], comparison["policy"]["reason"]))
    lines.append("")
    lines.append("## Channel metrics")
    lines.append("")
    header = ("| channel | sufficient | recalled | first ctx | full ctx | "
              "positions | extracted | usable | false-conf | chars |")
    lines.append(header)
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, stats in comparison["metrics"]["channels"].items():
        lines.append("| %s | %d/%d | %d/%d | %d/%d | %d/%d | %d/%d | %d/%d | "
                     "%d/%d | %d | %d |" % (
                         name,
                         *(stats["sufficient_evidence"]),
                         *(stats["evidence_recalled"]),
                         *(stats["in_first_context"]),
                         *(stats["in_full_context"]),
                         *(stats["positions_with_evidence"]),
                         *(stats["extraction_coverage"]),
                         *(stats["usable_coverage"]),
                         stats["false_confirmed_contradictions"],
                         stats["context_chars"]))
    lines.append("")
    lines.append("## Positions (all %d)" % len(comparison["positions"]))
    lines.append("")
    for position in comparison["positions"]:
        lines.append("### #%d (%s): %s" % (
            position["number"], position["scope"],
            ", ".join(position.get("expected_evidence", []))))
        for name in comparison["methods"]:
            channel = position["channels"][name]
            lines.append("- %s: rank=%s delivered=%s sufficient=[%s] chars=%d" % (
                name, ",".join(map(str, channel["ranking_1based"][:8])),
                ",".join(map(str, channel["context_pages_1based"])),
                ",".join(channel["sufficient"]),
                channel["context_chars"]))
        for channel_name in comparison["methods"]:
            channel = position["channels"][channel_name]
            for chain in channel["chains"]:
                lines.append(
                    "- #%d/%s/%s: %s%s%s" % (
                        position["number"], channel_name,
                        chain["evidence_id"], chain.get("loss") or "sufficient",
                        " bound" if chain.get("bound") else "",
                        (" source_id=%s page=%s" % (
                            chain.get("source_id", "?"),
                            chain.get("page_1based", "?")))))
        lines.append("")
    lines.append("Loss codes: extraction_missing, unusable_ocr, not_ranked, "
                 "context_dropped, binding_unresolved, validation_rejected, "
                 "measurement_basis_unresolved, source_conflict, "
                 "transport_error.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline evidence-retrieval benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="extract snapshots once")
    prepare.add_argument("--gold", required=True)
    prepare.add_argument("--output", required=True)
    compare = sub.add_parser("compare", help="rank the frozen corpus")
    compare.add_argument("--gold", required=True)
    compare.add_argument("--corpus", required=True)
    compare.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        return cmd_prepare(args)
    return cmd_compare(args)


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
