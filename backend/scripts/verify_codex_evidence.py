"""Recheck addressed totals and VOR candidates offline against a reviewed fixture."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import pymupdf

from codex_document_tool import Documents
from codex_vor_links import quantity_links
from pdf_targeted_cells import cached_cell_texts, corroborate_scope_text, verify_scope_binding
from vor_parse import load_vor_positions


def workbook_links(workbook):
    rows, _ = load_vor_positions(workbook.read_bytes(), workbook.name)
    inputs = []
    for row in rows:
        source = row.position_source
        calculation = row.calculation or {}
        inputs.append(dict(position_index=row.index, sheet=source.sheet, row=source.row,
            work_name=row.work_name, unit=row.unit, vor_value=row.volume, position_type=row.position_type,
            formula=row.formula, comment=row.comment, workbook_calculation=calculation,
            section_headings=[heading.model_dump() for heading in source.sections],
            excel_references=[ref for ref in calculation.get('dependencies', []) if ref.get('origin') == 'vor_cell']))
    links = quantity_links(inputs)
    return {str(item['row']): [link['row'] for link in links.get(item['position_index'], [])] for item in inputs}


def evaluate(root, pdf, workbook, baseline, cache):
    started = monotonic()
    (root / 'inputs').mkdir()
    source = root / 'inputs' / 'project.pdf'
    data = pdf.read_bytes()
    source.write_bytes(data)
    digest = sha256(data).hexdigest()
    (root / 'manifest.json').write_text(json.dumps({'documents': [
        dict(id='pd', kind='project', path='inputs/project.pdf', sha256=digest),
    ]}), encoding='utf-8')
    documents = Documents(root, cache)
    cells = []
    with pymupdf.open(source) as original:
        for fixture in baseline['cells']:
            page, scope, column, unit = fixture['query']
            result = json.loads(documents.call('find_project_total',
                dict(document_id='pd', page=page, scope=scope, column=column, unit=unit))['text'])
            verified = None
            independent_scope = None
            if result['status'] == 'read':
                key = result['evidence_id']
                observation = json.loads((root / 'codex-evidence' / f'{key}.json').read_bytes())
                options = dict(pdf_sha256=digest, cache_dir=cache / 'targeted-table-cache', bbox=observation['bbox'])
                verified = (
                    key == sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
                    and observation['quote'] in cached_cell_texts(page_number=page, **options)
                    and verify_scope_binding(original[page-1], binding=observation['scope_binding'], **options)
                    and all(0 <= value <= 1 for value in observation['highlight'])
                )
                if verified:
                    independent_scope = corroborate_scope_text(original[page-1], observation['scope_binding'])
            correct = (verified and result['reading']['text'] == fixture['expected_from_pdf']
                       if 'expected_from_pdf' in fixture
                       else result['status'] == fixture['expected_status'])
            cells.append(dict(query=fixture['query'], status=result['status'], correct=bool(correct),
                verified=verified, reading=result.get('reading'), candidates=result.get('candidates'),
                independent_scope_text=independent_scope,
                scope_binding=result.get('scope_binding'), evidence_id=result.get('evidence_id'),
                reason=result.get('reason'), header_rereads=result.get('header_rereads'),
                numeric_ocr_retries=result.get('numeric_ocr_retries')))
    links = workbook_links(workbook)
    reviewed = []
    for expected in baseline['manual_review_links']:
        actual = links[str(expected['row'])]
        parent = expected['parent']
        status = ('correct' if actual == [parent] else 'ambiguous' if parent in actual
                  else 'wrong' if actual else 'missed')
        reviewed.append(dict(row=expected['row'], parent=parent, actual=actual, status=status))
    return dict(cells=cells, links=reviewed, link_counts=dict(Counter(row['status'] for row in reviewed)),
                evidence_rejected=sum(row['verified'] is False for row in cells),
                false_evidence=sum(row['status'] == 'read' and not row['correct'] for row in cells),
                statuses=dict(Counter(row['status'] for row in cells)),
                header_rereads=sum(row['header_rereads'] or 0 for row in cells),
                numeric_ocr_retries=sum(row['numeric_ocr_retries'] or 0 for row in cells),
                tool_calls=len(cells),
                elapsed_seconds=monotonic() - started)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('pdf', 'workbook', 'baseline', 'cache', 'output'):
        parser.add_argument(f'--{name}', type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_bytes())
    with TemporaryDirectory() as directory:
        report = evaluate(Path(directory), args.pdf, args.workbook, baseline, args.cache)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    numeric = [row for row, fixture in zip(report['cells'], baseline['cells'], strict=True)
               if 'expected_from_pdf' in fixture]
    print(f"Verified cells: {sum(row['correct'] for row in numeric)}/{len(numeric)}")
    print(report['link_counts'], f"{report['elapsed_seconds']:.2f}s")
    if (any(not row['correct'] for row in report['cells'])
            or any(row['status'] != 'correct' for row in report['links'])):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
