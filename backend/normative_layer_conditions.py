"""Evaluate a cited layer-thickness condition against verified project facts.

Thresholds and clause IDs are read from the supplied normative text, not coded
for a project. The model must first confirm the source's applicability.
"""
import re
from decimal import Decimal

from normative_parameters import measure

TOPIC = 'Условие нормирования по подтверждённой толщине'


def _same_family(source,definition):
    families={match.casefold() for match in re.findall(r'\b(?:ГЭСН|ФЕР|ТЕР)[а-яё]*\b',source.get('label',''),re.I)}
    return families=={definition['family']}


def _selected_base(result,selected):
    from normative_thickness_rules import source_definition,adjustment_rule
    cards=result.get('parameter_cards',[])
    sources={source['id']:source for source in result['sources']}
    bases={}
    for proposal in result.get('parameter_proposals',[]):
        source=sources.get(proposal.get('source_id'))
        definition=source_definition(source) if source else None
        if (proposal.get('parameter')=='unit_scale' and proposal.get('scope_state')=='verified'
                and source and source['id'] in selected and definition
                and any(c['id']=='unit_scale:'+source['id'] and c['state']=='confirmed' for c in cards)):
            bases[source['id']]=(source,definition)
    # A separately verified measure for an explicit thickness correction is
    # not a second base. Keep unrelated/ambiguous definitions as blockers.
    adjustments={sid for sid,(_,definition) in bases.items()
        if any(other!=sid and adjustment_rule(definition,base)
            for other,(_,base) in bases.items())}
    bases={sid:value for sid,value in bases.items() if sid not in adjustments}
    return next(iter(bases.values())) if len(bases)==1 else None


def _compound_tail_proof(text,result,selected):
    """Do not close a compound predicate by proving only its layer prefix."""
    if re.fullmatch(r'\s*в\s+два\s+слоя[.\s]*',text,re.I):
        return [],''
    tail=re.fullmatch(r'\s*в\s+два\s+слоя\s+Устройство\s+основания\s+производится\s+'
        r'бульдозером\s+мощностью\s+(\d+(?:[.,]\d+)?)\s+кВт[.\s]*',text,re.I)
    base=_selected_base(result,selected)
    if not tail or not base:
        return None
    from normative_conditional_selection import _readable
    from normative_thickness_rules import CODE
    base_source,definition=base
    power=Decimal(tail[1].replace(',','.'))
    if power<=0:
        return None
    for source in result['sources']:
        if (source['kind']!='normative' or not _readable(source)
                or source['id']!=base_source['id']+':resources:'+str(source.get('page'))
                or source.get('source_id')!=definition['source_id']
                or source.get('version_id')!=definition['version_id']
                or not isinstance(source.get('page'),int) or source['page']<=0
                or set(re.findall(CODE,source.get('label','')))!={definition['code']}
                or not _same_family(source,definition)
                or 'ресурсы' not in source.get('label','').casefold()
                or not re.match(r'Ресурсы нормы '+re.escape(definition['code'])+r';',source['text'])):
            continue
        for line in source['text'].splitlines():
            resource=re.fullmatch(r'\s*\d{2}\.\d{2}\.\d{2}-\d{3}\s+Бульдозеры,\s*мощность\s+'
                r'(\d+(?:[.,]\d+)?)\s+кВт(?:\s*\(\d+(?:[.,]\d+)?\s+л\.с\.\))?;\s*'
                r'маш\.-ч:\s*(\d+(?:[.,]\d+)?)\s*',line,re.I)
            if (resource and Decimal(resource[1].replace(',','.'))==power
                    and Decimal(resource[2].replace(',','.'))>0):
                return [source['id']],f' Мощность бульдозера отдельно подтверждена ресурсом выбранной нормы: «{line.strip()}».'
    return None


def _referenced_condition_spans(position,result,selected):
    """A literal VOR clause may bind a fully validated selected base directly.

    This closes only the threshold/two-layer predicate. It cannot establish
    norm applicability, a material rate, or an unvalidated thickness adjustment.
    """
    from normative_conditional_selection import CONDITION,CODE,_named_in_work,_readable,_project_thickness,condition_scope_matches
    from normative_thickness_adjustment import findings
    cards=result.get('parameter_cards',[])
    if (result.get('pipeline_version',0)<4 or result.get('execution_state')!='complete'
            or not any(c['id']=='quantity' and c['state']=='confirmed' for c in cards)):
        return {}
    fact=_project_thickness(position,result,cards)
    if not fact or findings(position,result,{}, {},cards=cards):
        return {}
    base=_selected_base(result,selected)
    if not base:
        return {}
    definition=base[1]
    spans={}
    for source in result['sources']:
        if (source['kind']!='normative' or not _readable(source) or not _named_in_work(source,position)
                or source.get('source_id')!=definition['source_id'] or not _same_family(source,definition)):
            continue
        for condition in CONDITION.finditer(source['text']):
            allowed={re.sub(r'\s+','',code[0]) for code in re.finditer(CODE,condition['bases'])}
            if definition['code'] not in allowed:
                continue
            if not condition_scope_matches(condition['prefix'],fact):
                continue
            spans.setdefault(source['id'],[]).append(condition.span())
    return spans


def layer_condition_proof(position, result, group):
    if not group:
        return None
    from audit_predicates import technical_specs
    specs = technical_specs(position.work_name,getattr(position,'comment',''),
        getattr(position,'position_type',''),[],getattr(position,'formula',''))
    candidates = [s for s in specs
        if s.property_label in {'consumption','normative_instruction'} and re.match(r'в\s+два\s+слоя\b',s.expected_text,re.I)]
    thresholds = [s for s in specs if s.claim_id.startswith('geometry:comment:thickness:')]
    if not candidates:
        return None
    card = next((c for c in result['parameter_cards'] if c['id']=='thickness' and c['state']=='confirmed'),None)
    if not card:
        return None
    actual = measure(card['found_value'],card['found_unit'])
    if actual != measure(str(group['height']),'см'):
        return None
    from normative_conditional_selection import _project_thickness,condition_scope_matches
    fact=_project_thickness(position,result,result['parameter_cards'])
    if not fact:
        return None
    selected = {sid for item in result.get('items',[]) if item['state']=='confirmed'
        and item.get('project_source_ids') and 'normative:applicability' in item.get('claim_ids',[])
        for sid in item.get('normative_source_ids',[])}
    claims=[]
    resource_ids=[]
    explanations=[]
    for spec in candidates:
        notes=[s for s in specs if s.claim_id=='attribute:comment:separate_notes']
        # Old reviews treated these notes as one compound requirement. Preserve
        # that conservative proof gate while exposing separate tasks to agents;
        # only the same literal resource proof may close the new power claim.
        compound=spec.expected_text+'\n'+'\n'.join(s.expected_text for s in notes) if notes else spec.expected_text
        tail_proof=_compound_tail_proof(compound,result,selected)
        if tail_proof is not None:
            claims.append(spec.claim_id)
            claims.extend(s.claim_id for s in notes)
            resource_ids.extend(tail_proof[0])
            explanations.append(tail_proof[1])
    if not claims:
        return None
    referenced=_referenced_condition_spans(position,result,selected)
    for source in result['sources']:
        if source['kind']!='normative' or source['id'] not in selected and source['id'] not in referenced:
            continue
        for match in re.finditer(r'при\s+толщин\w*[^.!?]{0,220}?(более|свыше|не\s+менее|не\s+более)\s*'
                r'(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b[^.!?]{0,180}?нормир\w*\s+в\s+два\s+слоя',source['text'],re.I):
            if source['id'] not in selected and not any(start==match.start() and match.end()<=end
                    for start,end in referenced[source['id']]):
                continue
            limit=measure(match[2],match[3])
            if not condition_scope_matches(match[0],fact):
                continue
            if not actual or not limit or actual[0]!=limit[0]:
                continue
            operator=re.sub(r'\s+',' ',match[1].casefold())
            satisfied={'более':actual[1]>limit[1],'свыше':actual[1]>limit[1],
                'не менее':actual[1]>=limit[1],'не более':actual[1]<=limit[1]}[operator]
            if not satisfied:
                continue
            # This predicate is the normative threshold written in the comment,
            # not the constructed layer's actual thickness. Close it only when
            # both its operator and physical value match the cited condition.
            threshold_claims = []
            for spec in thresholds:
                condition = re.search(r'(более|свыше|не\s+менее|не\s+более)\s*'
                    r'(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b', spec.label, re.I)
                if (condition and re.sub(r'\s+', ' ', condition[1].casefold()) == operator
                        and measure(condition[2], condition[3]) == limit):
                    threshold_claims.append(spec.claim_id)
            if thresholds and not threshold_claims:
                continue
            return dict(topic=TOPIC,state='confirmed',required=True,claim_ids=claims+threshold_claims,
                project_source_ids=card['source_ids'],normative_source_ids=list(dict.fromkeys([source['id']]+resource_ids)),
                explanation=f"ПД: {card['found_value']} {card['found_unit']}. Условие источника: «{match[0]}». "
                    f"Условие выполнено; два технологических слоя ВОР {group['first']} + {group['second']} = {group['height']} см соответствуют проектной толщине."
                    +''.join(dict.fromkeys(explanations)))
    return None
