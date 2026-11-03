"""Property-specific navigation inside explicitly cited material standards.

These are lexical candidates, never equivalence or applicability decisions.
Expected quantities and formula operands are not used as search terms.
"""

import re
import unicodedata


STANDARD = re.compile(r'\bГОСТ\s*(?:Р\s*)?\d+(?:[.\-]\d+)*(?:\s*[-–]\s*\d{4})?', re.I)
PROPERTY = re.compile(r'(?<!\w)(?:М|M|К|K|F|В|B|W)\s*\d+(?:[.,]\d+)?(?!\w)')


def _property(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).upper().translate(
        str.maketrans({'М':'M', 'К':'K', 'В':'B'}))


def search(kb, position):
    text=position.work_name+'\n'+position.comment
    standards=list(dict.fromkeys(m[0].strip() for m in STANDARD.finditer(text)))[:2]
    properties={}
    for match in PROPERTY.finditer(text):
        properties.setdefault(_property(match[0]),match[0])
    plan=[(standard, prop, literal) for standard in standards
        for prop,literal in list(properties.items())[:3]][:4]
    output, seen, queries, pools=[], set(), [], []
    for standard, prop, literal in plan:
        query=standard+' '+literal
        hits=kb.search(query,kind='GOST',facts={},limit=3)
        selected=[]
        pool=[]
        for hit in hits:
            # FTS can return only the standard's other query terms. Report the
            # absence of the requested literal instead of calling it a hit.
            literals={_property(m[0]) for m in PROPERTY.finditer(hit.get('text') or '')}
            if prop not in literals:
                continue
            selected.append(hit['id'])
            pool.append(hit)
        pools.append(pool)
        queries.append(dict(query=query,property=prop,candidate_ids=selected,
            status='literal_candidates' if selected else 'no_literal_in_top_candidates'))
    # The first property must not fill the source budget before the others.
    for rank in range(3):
        for pool in pools:
            if rank>=len(pool):
                continue
            hit=pool[rank]
            key=(hit['entity_kind'],hit['id'])
            if key not in seen and len(output)<4:
                output.append(hit)
                seen.add(key)
    return dict(queries=queries,requires_applicability_check=True,
        warning='Разные обозначения свойств не эквивалентны без отдельного основания; совпадение марки не доказывает пригодность материала.'),output
