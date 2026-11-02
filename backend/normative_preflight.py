"""Bounded definition alternatives and literal same-table adjustment navigation."""
import re

from normative_agent import search_evidence


CODE = r'\d{2}-\d{2}-\d{3}-\d{2,3}'


def compound_operation_queries(query):
    """Navigation for explicit joined operations, retaining the literal object.

    Long descriptions can exhaust the search tokenizer before the final work
    operation. Only a bounded work-title grammar is recognized; material names,
    alternatives, exclusions and numeric/conditional descriptions stay intact.
    Inflection/synonym normalization changes query words only, never evidence.
    """
    if re.search(r'\b(?:без|не|кроме|исключая|либо|или|при|если)\b',query,re.I):
        return []
    head=re.match(r'^\s*(?:Устройство|Подготовка|Укладка|Посадка)\s+'
        r'(?P<object>[а-яё]+(?:[ -]+[а-яё]+){0,4}?)\s+(?:с|и)\s+',query,re.I)
    if not head or re.search(r'\b(?:из|по|для)\b',head['object'],re.I):
        return []
    operations={'засевом':'посев','посевом':'посев','поливом':'полив',
        'уплотнением':'уплотнение','укладкой':'укладка'}
    found=re.findall(r'\b(?:и|с)\s+('+ '|'.join(operations)+r')\b',query[head.end('object'):],re.I)
    return list(dict.fromkeys(operations[word.casefold()]+' '+head['object'] for word in found))[:2]


def scoped_query(query):
    return bool(re.search(r'(?:ГОСТ|СП|ПНСТ|ОДМ)\s*(?:Р\s*)?\d',query,re.I)
        or re.fullmatch(r'\d+(?:\.\d+)+',query.strip())
        or re.fullmatch(CODE,query.strip()))


def work_variants(kb,query,facts):
    """Two independently ranked definitions cannot be displaced by a clause."""
    if scoped_query(query):
        return search_evidence(kb,query,facts,limit=2)
    from normative_machine_query import machine_query
    query=machine_query(query)
    return [*kb.search(query,kind='GESN',facts=facts,limit=2),
        *kb.search(query,kind='ALL',facts=facts,limit=1)]


def adjustment_references(item,base):
    """Follow a literal reference/range, never infer a neighbour from its code."""
    from normative_thickness_rules import adjustment_rule
    return adjustment_rule(item,base) is not None


def same_table_adjustments(kb,bases,facts,*,limit=2):
    """Read bounded siblings and literal references to retrieved definitions.

    These are navigation candidates. No rule, thickness or applicability state
    is changed; original checks, statuses and citations remain attached.
    """
    groups={}
    for base in bases:
        key=tuple(base.get(field) for field in ('source_id','family','table_code'))
        if base.get('entity_kind')=='norm' and all(key):
            groups.setdefault(key,[]).append(base)
    output=[]
    for (source_id,family,table_code),group in list(groups.items())[:4]:
        with kb.connect() as db:
            rows=db.execute('SELECT id FROM norms WHERE source_id=? AND family=? AND table_code=? ORDER BY code,id LIMIT 33',
                (source_id,family,table_code)).fetchall()
        # Do not silently turn a truncated table into an exhaustive selection.
        if len(rows)>32:
            continue
        with kb.connect() as db:
            # A lexical reference is only navigation; the full literal link,
            # step and direction are validated below. Never guess a next table.
            cross=db.execute('SELECT id FROM norms WHERE source_id=? AND family=? AND table_code!=? '
                'AND text LIKE ? ORDER BY code,id LIMIT 33',(source_id,family,table_code,'%'+table_code+'%')).fetchall()
        if len(cross)<=32:
            rows=[*rows,*cross]
        for row in rows:
            item=kb.entity('norm',row['id'])
            if item.get('status')=='native_definition_ambiguous' or not any(adjustment_references(item,base) for base in group):
                continue
            item['checks'],item['missing_facts']=kb.checks(item,facts)
            item['rules']=kb.rules(item,facts)
            item['edition_confirmed']=False
            item['automatic_application_allowed']=False
            item['exact_code']=False
            output.append(item)
            if len(output)>=limit:
                return output
    return output


def preflight_candidates(kb,queries,facts,*,definition_queries=(),limit=12):
    # A material-name query must not fill the candidate budget before the
    # owning work's definitions have been retrieved.
    queries=list(dict.fromkeys(queries))
    leading=[]
    while queries and scoped_query(queries[0]):
        leading.append(queries.pop(0))
    queries=leading+[query for query in queries if query in definition_queries]+[
        query for query in queries if query not in definition_queries]
    found={}
    broad_ids=set()
    operation_queries=set()
    for query in queries:
        broad=query in definition_queries and not scoped_query(query)
        from normative_machine_query import machine_query
        search_query=query if scoped_query(query) else machine_query(query)
        hits=work_variants(kb,search_query,facts) if broad else search_evidence(kb,search_query,facts,limit=2)
        for item in hits:
            found.setdefault((item['entity_kind'],item['id']),item)
            if broad and item['entity_kind']=='norm':
                broad_ids.add(item['id'])
        if broad:
            for operation_query in compound_operation_queries(query):
                if operation_query in operation_queries or len(operation_queries)>=2:
                    continue
                operation_queries.add(operation_query)
                for item in kb.search(operation_query,kind='GESN',facts=facts,limit=2):
                    found.setdefault((item['entity_kind'],item['id']),item)
                    broad_ids.add(item['id'])
    reserve=min(2,max(0,limit-2))
    selected=list(found.values())[:limit-reserve]
    remaining=limit-len(selected)
    if remaining>0:
        bases=[item for item in selected if item['id'] in broad_ids]
        for item in same_table_adjustments(kb,bases,facts,limit=min(2,remaining)):
            key=(item['entity_kind'],item['id'])
            if not any((selected_item['entity_kind'],selected_item['id'])==key for selected_item in selected):
                selected.append(item)
                found.setdefault(key,item)
    selected_keys={(item['entity_kind'],item['id']) for item in selected}
    for key,item in found.items():
        if len(selected)>=limit:
            break
        if key not in selected_keys:
            selected.append(item)
            selected_keys.add(key)
    return selected
