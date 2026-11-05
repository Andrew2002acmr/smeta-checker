"""Prepare one normative PDF on the CPU into a new, inactive immutable release.

No edits to CURRENT, no database service, queue, model calls or cached verdicts.
Incomplete raster evidence is searchable but cannot certify a normative claim.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import shutil
import unicodedata
from uuid import uuid4

from pdf_inline_evidence import apply_transcriptions, coverage, inline_text, prepare_inline


def prepare_source(kb, source_id, *, recognize=False, transcriptions=()):
    import pymupdf

    with kb.connect() as db:
        source = dict(db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())
    path = kb.source_pdf(source_id)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != source['sha256']:
        raise ValueError('Normative PDF bytes changed')
    pages = []
    with pymupdf.open(path) as document:
        for index in range(len(document)):
            page = document[index]
            regions = prepare_inline(page, digest, recognize=recognize)
            if regions:
                pages.append(dict(page=index + 1, native_text=page.get_text(sort=True), regions=regions))
    all_regions = [region for page in pages for region in page['regions']]
    corrected = apply_transcriptions(all_regions, transcriptions)
    by_id = {r['source_id']: r for r in corrected}
    for page in pages:
        page['regions'] = [by_id[r['source_id']] for r in page['regions']]
        page['coverage'] = coverage(page['regions'])
    return dict(version='normative-inline-preparation-v1', kb=kb.identity,
        source_id=source_id, source_sha256=digest, pages=pages,
        model_calls=0, automatic_verdicts=0)


@contextmanager
def _staging_directory(parent):
    # tempfile's private 0700 directory installs an owner-only ACL on Windows.
    # A published corpus must inherit the same reader access as its siblings.
    parent = parent.resolve()
    staging = parent / ('.prepare-' + uuid4().hex)
    staging.mkdir(mode=0o755)
    try:
        yield staging
    finally:
        if staging.exists():
            if staging.resolve().parent != parent:
                raise ValueError('Staging cleanup target left release directory')
            shutil.rmtree(staging)


def stage_release(kb, report, release_id):
    """Keep old literals and add addressed image readings to their physical page.

    Page-wide incomplete clauses stay unreviewed. A visual transcription
    recovers characters only; it does not establish material compatibility.
    """
    if report['kb'] != kb.identity:
        raise ValueError('Preparation report belongs to another release')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', release_id):
        raise ValueError('Invalid release id')
    # Preparation may have been saved hours before publishing. A valid old
    # report cannot authorize changed source bytes or another source record.
    with kb.connect() as source:
        row = source.execute('SELECT sha256 FROM sources WHERE id=?', (report['source_id'],)).fetchone()
    if row is None or row[0] != report['source_sha256']:
        raise ValueError('Preparation report source identity changed')
    with kb.source_pdf(report['source_id']).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != report['source_sha256']:
            raise ValueError('Normative PDF bytes changed after preparation')
    directory = kb.root / 'releases' / release_id
    if directory.exists():
        raise FileExistsError(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    report = deepcopy(report)
    # No discoverable release or pin exists until all data, FTS and integrity
    # checks succeed. Temporary files stay on the same filesystem for rename.
    with _staging_directory(directory.parent) as staging:
        _write_release(kb, report, release_id, staging / 'knowledge.sqlite')
        target = staging / 'knowledge.sqlite'
        with target.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        pin = dict(database=(directory / target.name).relative_to(kb.root).as_posix(),
            release_id=release_id, profile=kb.profile, sha256=sha)
        (staging / 'pin.json').write_text(json.dumps(pin, ensure_ascii=False, indent=2), encoding='utf-8')
        (staging / 'preparation-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if directory.exists():
            raise FileExistsError(directory)
        staging.rename(directory)
    return pin


def _write_release(kb, report, release_id, target):
    # SQLite backup rather than a file copy also handles a pinned local cache.
    with closing(sqlite3.connect(target)) as dest, dest:
        with kb.connect() as source:
            source.backup(dest)
        rows = dest.execute('SELECT id,text,citations_json,status FROM clauses WHERE source_id=? ORDER BY id',
            (report['source_id'],)).fetchall()
        texts = {cid: text for cid, text, _, _ in rows}
        states = {cid: status for cid, _, _, status in rows}
        indexed = set()
        seen_pages = set()
        for page in report['pages']:
            if page['page'] in seen_pages:
                raise ValueError('Duplicate raster page in preparation report')
            seen_pages.add(page['page'])
            additions = inline_text(page['regions'])
            # Do not trust a stale/edited coverage flag over addressed regions.
            page['coverage'] = coverage(page['regions'])
            state = ('ocr_unreviewed_inline_raster' if page['coverage']['visual_review_required']
                else 'native_with_visual_transcriptions')
            matches = [(cid, json.loads(citations)) for cid, _, citations, _ in rows
                if any(ref.get('physical_page', ref.get('page')) == page['page'] for ref in json.loads(citations))]
            if not matches:
                raise ValueError('No source clause for detected raster page')
            # Prefer a physical-page clause; keep a multi-page clause intact if
            # that is the only available carrier. Never replace its old scope.
            carrier, _ = min(matches, key=lambda entry: len(entry[1]))
            texts[carrier] += (f"\n[Дополнение: физическая страница {page['page']}]\n" + additions
                + '\n[Исходный текстовый слой этой же страницы]\n' + page['native_text'])
            indexed.add(carrier)
            if states[carrier] != 'ocr_unreviewed_inline_raster':
                states[carrier] = state
            for cid, _ in matches:
                if page['coverage']['visual_review_required']:
                    states[cid] = state
            page['updated_clauses'] = len(matches)
            page['carrier_clause_id'] = carrier
        for cid, _, _, _ in rows:
            dest.execute('UPDATE clauses SET text=?,status=? WHERE id=?', (texts[cid], states[cid], cid))
        for cid in sorted(indexed):
            chunks = dest.execute("SELECT id FROM chunks WHERE entity_type='clause' AND entity_id=?", (cid,)).fetchall()
            if not chunks:
                raise ValueError('Missing search chunk for prepared clause')
            for (chunk,) in chunks:
                if dest.execute('SELECT count(*) FROM search_fts WHERE chunk_id=?', (chunk,)).fetchone()[0] != 1:
                    raise ValueError('Missing or duplicate search chunk index')
                dest.execute('UPDATE search_fts SET text=? WHERE chunk_id=?',
                    (unicodedata.normalize('NFKC', texts[cid]), chunk))
        dest.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',
            ('release_id', json.dumps(release_id)))
        dest.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',
            ('inline_preparation', json.dumps(report, ensure_ascii=False)))
        if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Derived normative release integrity check failed')


def main():
    import argparse
    from normative_corpus import Corpus

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--ocr', action='store_true')
    parser.add_argument('--transcriptions', type=Path)
    parser.add_argument('--release-id')
    args = parser.parse_args()
    kb = Corpus(args.root)
    readings = json.loads(args.transcriptions.read_text(encoding='utf-8')) if args.transcriptions else []
    report = prepare_source(kb, args.source_id, recognize=args.ocr, transcriptions=readings)
    if args.release_id:
        report['staged_release'] = stage_release(kb, report, args.release_id)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(source_id=args.source_id, image_pages=len(report['pages']),
        regions=sum(len(p['regions']) for p in report['pages']), staged_release=report.get('staged_release'),
        model_calls=0), ensure_ascii=False))


if __name__ == '__main__':
    main()
