"""Repeated validation must not grow prose or modify literal source evidence."""
from copy import deepcopy

import pytest

from normative_position_check import CheckedItem, validate_items
from normative_verdict import readable_review


@pytest.mark.parametrize('project_ids,normative_ids', [
    (['unknown'], ['n']),
    (['p'], ['unknown']),
    (['p'], []),
])
def test_unsupported_review_is_stable_across_repeated_validation(project_ids, normative_ids):
    sources = [dict(id='p', kind='project', page=7, text='s0 is literal source text'),
               dict(id='n', kind='normative', label='Source', page=2, text='Unchanged norm')]
    item = CheckedItem(topic='Applicability', state='confirmed', claim_ids=['normative:applicability'],
                       explanation='Review s0 against s1.', project_source_ids=project_ids,
                       normative_source_ids=normative_ids)
    original = deepcopy(sources)
    first = readable_review(dict(summary='Sources s0+s1', sources=sources,
                                 items=validate_items([item], sources)))
    repeated = first
    for _ in range(4):
        repeated = readable_review(dict(repeated, items=validate_items(
            [CheckedItem.model_validate(v) for v in repeated['items']], sources)))
        assert repeated == first
    assert first['items'][0]['state'] == 'needs_facts'
    assert first['items'][0]['explanation'].count('Не хватает достоверных ссылок') == 1
    assert sources == original and first['sources'] == original

def test_unreviewed_normative_ocr_is_navigation_not_confirmed_evidence():
    sources = [dict(id='p', kind='project', origin='native'),
               dict(id='n', kind='normative', origin='ocr_unreviewed')]
    item = CheckedItem(topic='Scope', state='confirmed', claim_ids=['normative:applicability'],
                       explanation='OCR candidate', project_source_ids=['p'], normative_source_ids=['n'])
    checked = validate_items([item], sources)
    assert checked[0]['state'] == 'needs_facts'
    assert 'непроверенным OCR' in checked[0]['explanation']
    assert validate_items([CheckedItem.model_validate(checked[0])], sources) == checked
