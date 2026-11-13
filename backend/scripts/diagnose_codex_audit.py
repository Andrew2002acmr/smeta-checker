"""Compare a recovered Codex response with the persisted validation result."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

from codex_contract import (
    DIRECTIONS, DirectionConclusion, _operands_cited, _verify_calculation, aggregate_status,
)


def findings(row):
    result = {name: row[name] for name in DIRECTIONS}
    result.update({f"{item['direction']}:{ordinal}": item['finding']
                   for ordinal, item in enumerate(row.get('parameter_findings', []), 1)})
    return result


def agent_cause(finding):
    if finding['status'] not in {'needs_facts', 'not_checked'}:
        return None
    text = finding['conclusion'].casefold()
    if finding['status'] == 'needs_facts':
        if re.search(r'применимост|област[ьи] норм|услови[ея] примен', text):
            return 'applicability_unresolved'
        return 'required_source_missing'  # Agent-reported absence, not independent proof.
    if re.search(r'ocr|распознаван', text):
        return 'tool_unreadable'
    if re.search(r'не найден|не найдено', text):
        return 'tool_not_found'
    if re.search(r'применимост|област[ьи] норм|услови[ея] примен', text):
        return 'applicability_unresolved'
    return 'agent_unfinished'


def diagnose(raw, stored, events, roles):
    if any(raw[key] != stored[key] for key in ('project_id', 'vor_version_id')):
        raise ValueError('Input reports refer to different documents')
    by_index = {row['position_index']: row for row in raw['positions']}
    rejections = defaultdict(set)
    bad_refs = defaultdict(set)
    for event in events:
        if event.get('event') == 'validation.direction_rejected':
            rejections[event['position_index'], event['direction']].update(event['reasons'])
        elif event.get('event') == 'validation.citation_rejected':
            bad_refs[event['position_index'], event['direction']].add(event['citation_index'])
    rows, rejected = [], []
    for row in stored['positions']:
        index = row['position_index']
        original = by_index[index]
        if (original['sheet'], original['row']) != (row['sheet'], row['row']):
            raise ValueError('Position identity mismatch')
        before, after = findings(original), findings(row)
        entries = []
        for name in dict.fromkeys([*before, *after]):
            initial = before.get(name)
            saved = after.get(name)
            key = (index, name)
            reasons = sorted(rejections[key])
            rejected_citations = sorted(bad_refs[key])
            backend_causes = []
            if rejected_citations or {'citation', 'partial_citation'}.intersection(reasons):
                backend_causes.append('citation_rejected')
            if 'calculation' in reasons:
                backend_causes.append('calculation_rejected')
            entry = dict(direction=name, agent=initial, persisted=saved,
                         agent_cause=agent_cause(initial) if initial else None,
                         backend_causes=backend_causes, validation_reasons=reasons,
                         question_role=roles.get('rows', roles).get(str(row['row']), {}).get(name,
                             'additional_reported_by_agent' if ':' in name and (initial or saved)[
                                 'parameter'].startswith('Дополнительный вопрос — ') else 'required_or_unclassified'))
            entries.append(entry)
            if reasons or rejected_citations:
                detail = dict(row=row['row'], **entry)
                if initial and 'calculation' in reasons:
                    check = DirectionConclusion.model_validate(initial)
                    arithmetic_valid = _verify_calculation(check)
                    detail.update(decimal_arithmetic_valid=arithmetic_valid,
                                  operands_match_after_fix=arithmetic_valid and _operands_cited(check))
                    if detail['decimal_arithmetic_valid'] and detail['operands_match_after_fix']:
                        detail['classification'] = 'decimal_representation_in_addressed_operand'
                for source_index in rejected_citations:
                    sources = initial.get('sources', []) if initial else []
                    source = sources[source_index] if 0 <= source_index < len(sources) else None
                    reference = dict(index=source_index, source=source)
                    if source and source['kind'] == 'normative' and not re.fullmatch(
                        r'norm:[0-9a-f]{64}', source['document_id'],
                    ):
                        reference['classification'] = 'malformed_pinned_document_id'
                    detail.setdefault('rejected_sources', []).append(reference)
                rejected.append(detail)
        rows.append(dict(row=row['row'], position_index=index, work_name=row['work_name'],
                         agent_status=aggregate_status(f['status'] for f in before.values()),
                         persisted_status=aggregate_status(f['status'] for f in after.values()),
                         findings=entries))
    summary = {k: dict(Counter(row[k] for row in rows)) for k in ('agent_status', 'persisted_status')}
    for kind, causes in (('agent_cause', lambda item: [item['agent_cause']] if item['agent_cause'] else []),
                         ('backend_cause', lambda item: item['backend_causes'])):
        summary[kind] = dict(Counter(cause for row in rows for item in row['findings'] for cause in causes(item)))
        summary[f'{kind}_by_direction'] = {direction: dict(Counter(
            cause for row in rows for item in row['findings'] if item['direction'].split(':')[0] == direction
            for cause in causes(item))) for direction in DIRECTIONS}
    summary['findings_total'] = sum(len(row['findings']) for row in rows)
    summary['parameter_findings_total'] = sum(':' in item['direction'] for row in rows for item in row['findings'])
    summary['finding_statuses'] = {
        side: {direction: dict(Counter(item[side]['status'] for row in rows for item in row['findings']
                                      if item[side] and item['direction'].split(':')[0] == direction))
               for direction in DIRECTIONS}
        for side in ('agent', 'persisted')
    }
    return dict(project_id=stored['project_id'], vor_version_id=stored['vor_version_id'],
                summary=summary, positions=rows, rejections=rejected,
                question_review=roles.get('review'),
                agent_cause_basis='Agent narrative, not a position-level tool trace')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('raw', 'stored', 'events', 'roles', 'output'):
        parser.add_argument(f'--{name}', type=Path, required=True)
    args = parser.parse_args()
    report = diagnose(json.loads(args.raw.read_bytes()), json.loads(args.stored.read_bytes()),
                      [json.loads(line) for line in args.events.read_bytes().splitlines()],
                      json.loads(args.roles.read_bytes()))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
