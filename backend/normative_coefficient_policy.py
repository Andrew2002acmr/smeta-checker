"""Separate estimator values from normative conditions and site measurements.

Legacy policies remain replayable. New checks accept an explicit estimate
allowance within a proven applicable range, without inventing a measured value.
Geometry, material compatibility and contradictions retain their own checks.
"""
import ast
from copy import deepcopy
import re

TRACE = 'named_coefficient_policy_v1'
STRICT = 'independent_evidence_required'
CONDITIONAL = 'estimator_value_normative_conditions'
TOPIC = 'Назначение коэффициента принято'
PATTERN = re.compile(r'(?P<label>к[\s_.-]*упл\.?|к[\s_.-]*разр\.?|'
    r'коэффициент\s+(?:запаса\s+на\s+)?(?:уплотнения|разрыхления|расхода)|'
    r'коэффициент\s+(?P<purpose>[а-яё]+(?:[ -][а-яё]+){0,5}))'
    r'\s*[:=–—-]?\s*(?P<value>\d+(?:[.,]\d+)?)\s*', re.I)
CONDITIONAL_PURPOSE = re.compile(r'\b(?:при|если|только|для|кроме|без|свыше|менее|более|не|до|после)\b', re.I)


def compaction_basis(position, result, entry):
    """Check an estimator's design allowance, not a measured site coefficient.

    The SP78 mixture branch is deliberately narrow. Bounds come from the
    registered literal; stone/slag and unknown qualifiers remain unresolved.
    No source or project geometry is manufactured by this policy.
    """
    from normative_parameters import clean, number, material_fractions
    from vor_material_links import _family
    if ('уплотнение' not in entry['purpose'] or _family(position.work_name) != 'щпс'
            or number(entry['value']) not in _product(position)):
        return None
    sources = {s['id']: s for s in result.get('sources', [])}
    if any(c.get('id') == f"factor:{position.index}:{entry['value']}"
            and c.get('state') == 'possible_mismatch' for c in result.get('parameter_cards', [])):
        return None
    geometry = [c for c in result.get('parameter_cards', [])
        if c['id'] in {'quantity', 'thickness'} and c['state'] == 'confirmed']
    project = [sources[sid] for c in geometry for sid in c.get('source_ids', [])
        if sid in sources and sources[sid].get('kind') == 'project']
    wanted = material_fractions(position.work_name)
    if not any(_family(s.get('text', '')) == 'щпс' and wanted
            and material_fractions(s.get('text', '')) == wanted for s in project):
        return None
    proofs = []
    # Recognize the amended clause's complete branch rather than grabbing
    # any two decimals from a nearby range or a different material.
    pattern = (r'для песчано-гравийных \(щебеночных\) смесей и щебня фракций '
        r'\d+(?:[.,]\d+)?\s*[-–—]\s*\d+(?:[.,]\d+)? и '
        r'\d+(?:[.,]\d+)?\s*[-–—]\s*\d+(?:[.,]\d+)? мм марки по '
        r'дробимости (?P<strength>\d+) и более коэффициент запаса материала на '
        r'уплотнение следует ориентировочно принимать (?P<lo>\d+[.,]\d+)'
        r'\s*[-–—]\s*(?P<hi>\d+[.,]\d+), а для щебня')
    strength = re.search(r'(?<!\w)[мm]\s*(\d+)(?!\d)', position.work_name, re.I)
    for source in sources.values():
        text = clean(source.get('text', ''))
        label = clean(source.get('label', ''))
        if (source.get('kind') != 'normative' or not source.get('source_id')
                or not source.get('page')
                or not source.get('origin')
                or source.get('origin', '').startswith('ocr')
                or source.get('origin') in {'uncertain_table', 'native_definition_ambiguous'}
                or not re.search(r'сп\s*78\.13330\.2012', label)
                or not re.match(r'10\.2\b', text)):
            continue
        match = re.search(pattern, text)
        if not match or not strength or int(strength[1]) < int(match['strength']):
            continue
        lo, hi, value = number(match['lo']), number(match['hi']), number(entry['value'])
        if lo is None or hi is None or value is None or not lo <= value <= hi:
            continue
        proofs.append(dict(source_id=source['id'], normative_pdf_id=source['source_id'], quote=source['text'],
            lower=str(lo), upper=str(hi), project_source_ids=sorted({s['id'] for s in project}),
            material_strength_basis='VOR specification; project compatibility checked separately',
            purpose='estimated_material_allowance', actual_site_coefficient_proven=False))
    return proofs[0] if proofs and len({(p['lower'], p['upper']) for p in proofs}) == 1 else None


def _conditional_apply(position, result):
    output = deepcopy(result)
    entries = named(position)
    proofs = [(entry, compaction_basis(position, output, entry)) for entry in entries]
    proofs = [(entry, proof) for entry, proof in proofs if proof]
    if not proofs:
        return output
    originals = []
    accepted = accepted_claims(position, output)
    proven = {entry['value']: proof for entry, proof in proofs}
    for card in output.get('parameter_cards', []):
        proof = proven.get(card.get('declared_value'))
        if (proof and card.get('id', '').startswith(f'factor:{position.index}:')
                and card.get('state') == 'needs_facts'):
            originals.append(deepcopy(card))
            card.update(state='confirmed', source_ids=[proof['source_id']], quote=proof['quote'],
                source_anchors=[dict(source_id=proof['source_id'], quote=proof['quote'], rects=[])],
                reason='Сметное значение из примечания входит в применимый ориентировочный диапазон '
                    f"СП78 п.10.2: {proof['lower']}–{proof['upper']}. Фактический коэффициент на стройке не утверждается.")
    items = []
    for item in output.get('items', []):
        affected = set(item.get('claim_ids', [])) & accepted
        if not affected or item.get('state') != 'needs_facts':
            items.append(item)
            continue
        originals.append(deepcopy(item))
        remaining = [claim for claim in item['claim_ids'] if claim not in affected]
        if remaining:
            items.append({**item, 'claim_ids': remaining})
        items.append(dict(topic=TOPIC, state='confirmed', required=item.get('required', True),
            claim_ids=sorted(affected),
            project_source_ids=sorted({sid for _, proof in proofs for sid in proof['project_source_ids']}),
            normative_source_ids=[proof['source_id'] for _, proof in proofs],
            explanation='Коэффициент из примечания принят как сметное значение; назначение и '
                'ориентировочный диапазон проверены по СП78 п.10.2. Протокол пробной укатки '
                'для проверки сметного значения не требуется. Совместимость материала проверяется отдельно.'))
    output['items'] = items
    if originals:
        gaps = [c.get('label', c['id'])+': '+c['reason'] for c in output.get('parameter_cards', [])
            if c.get('required', True) and c['state'] != 'confirmed']
        gaps += [i['explanation'] for i in items if i.get('required', True) and i['state'] != 'confirmed']
        output['summary'] = (' '.join(gaps) or 'Сметный коэффициент проверен по применимому диапазону.')[:2500]
    output['evidence_trace'] = [e for e in output.get('evidence_trace', []) if e.get('kind') != TRACE]
    output['evidence_trace'].append(dict(kind=TRACE, policy=CONDITIONAL, entries=entries,
        accepted_claims=sorted(accepted), original_checks=originals,
        normative_value_proven=False, clarification_deferred=False,
        normative_conditions_proven=True, estimator_value_in_normative_range=True,
        normative_exact_value_claimed=False, range_proofs=[proof for _, proof in proofs]))
    return output


def _comment_project_basis(position, result):
    """The remaining ГОСТ/category/mark line must match a verified PD scope.

    Do not keep a compound comment open solely for its now-proven allowance;
    do not waive any unrecognized extra condition or the separate M800 claim.
    """
    from normative_parameters import clean, material_fractions
    entries = {entry['literal'] for entry in named(position)}
    lines = [line.strip() for line in re.split(r'[\n;]+', position.comment)
        if line.strip() and line.strip() not in entries]
    if len(lines) != 1:
        return []
    line = clean(lines[0])
    match = re.fullmatch(r'(?P<gost>гост\s+р\s+\d+(?:\.\d+)*-\d{4})\s+'
        r'категория\s+[кk](?P<category>\d+),?\s+марка\s+[мm](?P<mark>\d+)', line)
    if not match:
        return []
    sources = {s['id']: s for s in result.get('sources', [])}
    verified_ids = {sid for c in result.get('parameter_cards', [])
        if c['id'] in {'quantity', 'thickness'} and c['state'] == 'confirmed' for sid in c.get('source_ids', [])}
    for proposal in result.get('parameter_proposals', []):
        source = sources.get(proposal.get('source_id'))
        scope = clean(proposal.get('scope_quote', ''))
        if (not source or source['id'] not in verified_ids or source['kind'] != 'project'
                or proposal.get('scope_state') != 'verified' or not scope
                or scope not in clean(source['text'])
                or material_fractions(scope) != material_fractions(position.work_name)):
            continue
        if (match['gost'] in scope
                and re.search(r'категори\w*\s+[кk]'+match['category']+r'\b', scope)
                and re.search(r'марк\w*\s+[мm]'+match['mark']+r'\b', scope)):
            return [source['id']]
    return []


def named(position):
    from normative_parameters import number
    output=[]
    for line in re.split(r'[\n;]+',getattr(position,'comment','')):
        match=PATTERN.fullmatch(line.strip())
        if not match or (match['purpose'] and CONDITIONAL_PURPOSE.search(match['purpose'])):
            continue
        value=number(match['value'])
        if value is None or value<=0:
            continue
        label=match['label'].casefold()
        purpose=(label if match['purpose'] else
            'коэффициент запаса материала на уплотнение' if 'упл' in label else
            'коэффициент разрыхления' if 'разр' in label else 'коэффициент расхода материала')
        entry=dict(literal=line.strip(),value=str(value),purpose=purpose)
        entry['value_basis'] = dict(kind='estimator_provided',
            statement='Явно указанный коэффициент предоставлен сметчиками; отсутствие их нормативного источника не является ошибкой ВОР.',
            document_reference=None, independently_verified=False)
        output.append(entry)
    return output


def compaction_sources(kb, position):
    """Use the existing corpus search/registry for the named allowance's rule."""
    if not any('уплотнение' in entry['purpose'] for entry in named(position)):
        return []
    from normative_service import candidate
    from normative_source_registry import full_text_handle
    output = []
    for hit in kb.search('СП 78.13330.2012 10.2', kind='ALL', facts={}, limit=3):
        item = candidate(kb, hit, {})
        if item['code'] != '10.2' or not item['citations']:
            continue
        ref = item['citations'][0]
        output.append(dict(id=full_text_handle('coefficient:'+str(item['id']), item['text']),
            kind='normative', source_id=ref['source_id'], page=ref['page'],
            label=item['designation']+' · '+item['code'], origin=item['extraction_status'], text=item['text']))
    return output


def _product(position):
    from normative_parameters import number
    expression=(getattr(position,'calculation',None) or {}).get('expression') or getattr(position,'formula','')
    text=expression.strip().lstrip('=').replace(',','.')
    try:
        tree=ast.parse(text,mode='eval').body
    except (ValueError,SyntaxError):
        return []
    leaves=[]
    def visit(node):
        if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mult):
            return visit(node.left) and visit(node.right)
        if isinstance(node,ast.Constant) and not isinstance(node.value,bool):
            value=number(ast.get_source_segment(text,node))
            if value is not None and value>0:
                leaves.append(value)
                return True
        return False
    return leaves if visit(tree) else []


def accepted_claims(position,result):
    """Only an explicit named multiplier, not arbitrary resource consumption."""
    if result.get('coverage', {}).get('coefficient_policy') == STRICT:
        return set()
    from normative_parameters import number,measure
    entries=named(position)
    if result.get('coverage', {}).get('coefficient_policy') == CONDITIONAL:
        entries = [entry for entry in entries if compaction_basis(position, result, entry)]
    if not entries:
        return set()
    claims=set()
    comment=getattr(position,'comment','').strip()
    if len(entries)==1 and entries[0]['literal']==comment:
        claims.update({'basis:comment:consumption:1','attribute:comment:condition','comment'})
    elif result.get('coverage', {}).get('coefficient_policy') == CONDITIONAL and _comment_project_basis(position, result):
        claims.add('attribute:comment:condition')
    leaves=_product(position)
    quantity=next((c for c in result.get('parameter_cards',[]) if c['id']=='quantity'),{})
    parent=measure('1',quantity.get('unit',''))
    material=measure('1',getattr(position,'unit',''))
    for entry in entries:
        factor=number(entry['value'])
        if leaves.count(factor)!=1 or not parent or not material:
            continue
        # A same-dimension work amount × named factor, or an area × depth ×
        # named factor. Remaining geometry cards still gate the complete row.
        same=parent==material and len(leaves)==2
        volume=(parent[0]=='area' and material[0]=='volume' and len(leaves)==3
            and 'уплотнение' in entry['purpose'])
        if same or volume:
            claims.add('derived:rate')
    return claims


def apply(position,result):
    from normative_parameters import number
    entries=named(position)
    if not entries:
        return result
    if result.get('coverage', {}).get('coefficient_policy') == CONDITIONAL:
        return _conditional_apply(position, result)
    output=deepcopy(result)
    if output.get('coverage', {}).get('coefficient_policy') == STRICT:
        # A name/value supplied by an estimator is provenance, not independent
        # support for the dependent material rate. Keep unrelated facts intact.
        factor_cards = [c for c in output.get('parameter_cards', [])
            if c.get('id', '').startswith(f'factor:{position.index}:')
            and c.get('role') == 'coefficient']
        leaves = _product(position)
        needed = [entry for entry in entries if number(entry['value']) in leaves]
        unresolved = (any(c.get('state') != 'confirmed' for c in factor_cards)
            or any(not any(c.get('state') == 'confirmed' and c.get('source_ids')
                and number(c.get('declared_value')) == number(entry['value']) for c in factor_cards)
                for entry in needed))
        items = []
        originals = []
        for item in output.get('items', []):
            if unresolved and 'derived:rate' in item.get('claim_ids', []) and item.get('state') == 'confirmed':
                originals.append(deepcopy(item))
                remaining = [claim for claim in item['claim_ids'] if claim != 'derived:rate']
                if remaining:
                    items.append({**item, 'claim_ids': remaining})
                item = {**item, 'claim_ids': ['derived:rate'], 'state': 'needs_facts',
                    'explanation': item['explanation'] +
                        ' Независимое основание коэффициента не подтверждено; расход материала остаётся открытым.'}
            items.append(item)
        output['items'] = items
        output['evidence_trace'] = [e for e in output.get('evidence_trace', []) if e.get('kind') != TRACE]
        output['evidence_trace'].append(dict(kind=TRACE, policy=STRICT, entries=entries,
            accepted_claims=[], normative_value_proven=False, clarification_deferred=False,
            original_checks=originals))
        return output
    previous_gaps=[c for c in output.get('parameter_cards',[])
        if c.get('required',True) and c.get('state')!='confirmed']
    previous_summary=' '.join(c['label']+': '+c['reason'] for c in previous_gaps
        if 'label' in c and 'reason' in c)[:2500]
    originals=[]
    for card in output.get('parameter_cards',[]):
        if (card.get('role')!='coefficient' or card.get('state')!='needs_facts'
                or not card.get('id','').startswith(f'factor:{position.index}:')):
            continue
        entry=next((e for e in entries if number(e['value'])==number(card.get('declared_value'))),None)
        if entry:
            originals.append(deepcopy(card))
            basis=entry.get('value_basis')
            explanation=('Значение предоставлено сметчиками по их нормативной базе; '
                'документ-основание не приложен, независимая нормативная проверка не выполнена.'
                if basis else 'Значение временно принято по правилу пользователя; источник значения требует уточнения.')
            card.update(state='confirmed',reason=entry['purpose'].capitalize()+'. '+explanation)
    accepted=accepted_claims(position,output)
    items=[]
    for item in output.get('items',[]):
        if item.get('topic')==TOPIC:
            if set(item.get('claim_ids',[]))<=accepted:
                items.append(item)
            continue
        affected=set(item.get('claim_ids',[]))&accepted
        if not affected or item.get('state')!='needs_facts':
            items.append(item)
            continue
        remaining=[c for c in item['claim_ids'] if c not in affected]
        originals.append(deepcopy(item))
        if remaining:
            items.append({**item,'claim_ids':remaining})
        estimator_note=(' Коэффициент предоставлен сметчиками; '
            'название их нормативного источника пока не установлено.'
            if any(e.get('value_basis') for e in entries) else '')
        items.append(dict(topic=TOPIC,state='confirmed',required=True,claim_ids=sorted(affected),
            project_source_ids=[],normative_source_ids=[],
            explanation='Назначение явно указанного коэффициента распознано. Его значение временно принято '
                'по правилу пользователя.'+estimator_note+
                ' Это допущение не подтверждает геометрию, свойства материала или выбор нормы.'))
    output['items']=items
    if previous_summary and output.get('summary')==previous_summary:
        gaps=[c for c in output.get('parameter_cards',[])
            if c.get('required',True) and c.get('state')!='confirmed']
        output['summary']=(' '.join(c['label']+': '+c['reason'] for c in gaps) or
            ' '.join(i.get('explanation','') for i in items
                if i.get('required',True) and i.get('state')!='confirmed') or
            'Числовые основания проверены; коэффициент принят по основанию пользователя.')[:2500]
    output['evidence_trace']=[e for e in output.get('evidence_trace',[]) if e.get('kind')!=TRACE]
    output['evidence_trace'].append(dict(kind=TRACE,policy='temporary_user_acceptance',
        entries=entries,accepted_claims=sorted(accepted),original_checks=originals,
        normative_value_proven=False,clarification_deferred=True))
    return output
