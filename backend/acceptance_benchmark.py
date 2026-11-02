"""Offline, aspect-level acceptance checks over immutable review exports.

No application imports, retrieval, model calls or database access. Expectations
are supplied independently of the evaluator and never used to alter verdicts.
"""
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any


def evaluate(export, specification) -> dict[str, Any]:
    rows = export['rows']
    by_index = {row['index']: row for row in rows}
    if len(by_index) != len(rows):
        raise ValueError('Duplicate position index')
    cases = specification['cases']
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Duplicate case id')
    results = []
    for case in cases:
        row = by_index.get(case['index'])
        if row is None:
            results.append(dict(id=case['id'], index=case['index'], status='missing'))
            continue
        answer = row['payload']['position_check']
        checks = []
        for expectation in case['expectations']:
            kind = expectation['kind']
            if kind == 'state':
                passed = answer['state'] == expectation['expected']
            elif kind == 'card':
                cards = [c for c in answer.get('parameter_cards', []) if c['id'] == expectation['id']]
                passed = len(cards) == 1 and cards[0]['state'] == expectation['state']
                if passed and 'value' in expectation:
                    try:
                        passed = Decimal(str(cards[0].get('found_value'))) == Decimal(expectation['value'])
                    except InvalidOperation:
                        passed = False
            elif kind == 'forbidden_item':
                passed = not any(item['state'] == expectation['state']
                    and expectation['claim'] in item.get('claim_ids', [])
                    and item.get('required', True)
                    for item in answer.get('items', []))
            else:
                raise ValueError('Unknown expectation: ' + kind)
            checks.append(dict(expectation=expectation, passed=passed))
        if not checks:
            raise ValueError('Case has no expectations')
        results.append(dict(id=case['id'], index=case['index'], review_id=row['review_id'],
            status='passed' if all(c['passed'] for c in checks) else 'failed', checks=checks))
    counts = Counter(result['status'] for result in results)
    return dict(schema=1, identity=export.get('identity'), cases=results, counts=dict(counts),
        acceptance_passed=bool(cases) and counts['passed'] == len(cases), model_requests=0,
        note='Missing cases are coverage gaps, never successful checks. This is not a full-project accuracy estimate.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--specification', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {args.export.resolve(), args.specification.resolve()}:
        parser.error('Output must not overwrite inputs')
    evidence = args.export.read_bytes()
    specification = args.specification.read_bytes()
    report = evaluate(json.loads(evidence), json.loads(specification))
    report['input_sha256'] = dict(export=hashlib.sha256(evidence).hexdigest(),
        specification=hashlib.sha256(specification).hexdigest())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(counts=report['counts'], acceptance_passed=report['acceptance_passed'], model_requests=0)))
    return 0 if report['acceptance_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
