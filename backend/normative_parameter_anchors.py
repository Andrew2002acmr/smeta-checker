"""Typed literal anchors from supplied sources; never engineer applicability."""
import re

from normative_parameters import measure, quoted, construction_conflict, unit_scale_quoted, clean


def thickness_adjustments(work, sources):
    """Arithmetic candidates from literal norm titles, not applicability proofs."""
    from normative_thickness_rules import source_definition,base_thickness,adjustment_rule,literal_measure
    target=re.search(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b',work,re.I)
    if not target:
        return []
    target_measure=measure(target[1],target[2])
    if not target_measure:
        return []
    target_cm=target_measure[1]*100
    norms=[s for s in sources if s['kind']=='normative' and 'ресурсы' not in s.get('label','')]
    output=[]
    for base in norms:
        definition=source_definition(base)
        amount=base_thickness(base['text'])
        if not definition or not amount:
            continue
        base_unit=literal_measure(base,definition['code'])
        if not base_unit:
            continue
        for step in norms:
            other=source_definition(step)
            rule=adjustment_rule(other,definition) if other else None
            if not rule or not other or literal_measure(step,other['code'])!=base_unit:
                continue
            count=(target_cm/100-amount['value'])/rule['step']
            if (count!=count.to_integral_value() or count>0 and not rule['allow_positive']
                    or count<0 and not rule['allow_negative']):
                continue
            output.append(dict(base_source_id=base['id'],adjustment_source_id=step['id'],
                base_quote=amount['quote'],adjustment_quote=rule['reference_quote'],target_cm=str(target_cm),
                base_cm=str(amount['value']*100),step_cm=str(rule['step']*100),signed_steps=str(count),
                scope_verified=False,explanation='Только арифметика: применимость нормы и возможность уменьшения/увеличения проверь по пункту техчасти.'))
    return output[:12]


def parameter_candidates(sources):
    result = []
    for source in sources:
        text = source['text']
        if source['kind']=='project' and source.get('origin')=='ocr_verified_header':
            for fact in source.get('verified_parameters',[]):
                if fact.get('parameter')=='thickness' and quoted(fact['value'],fact['quote'],text):
                    result.append(dict(parameter='thickness',value=fact['value'],unit=fact['unit'],
                        source_id=source['id'],quote=fact['quote'],scope_quote=text,scope_state='unresolved'))
        if source['kind'] == 'project' and source.get('origin') == 'native':
            roles = list(re.finditer(r'(?:нижн\w*|средн\w*|верхн\w*)\s+сло[йя]\s+основан\w*',text,re.I))
            for match in re.finditer(r'\bh\s*=\s*(\d+(?:[.,]\d+)?)\s*(мм|см|м)\b',text,re.I):
                prior = [r for r in roles if 0 < match.start()-r.start() < 800]
                if not prior:
                    continue
                role = prior[-1]
                scope = text[role.start():match.end()]
                if len(scope) <= 900:
                    result.append(dict(parameter='thickness',value=match[1].replace(',','.'),unit=match[2].lower(),
                        source_id=source['id'],quote=match[0],scope_quote=scope,scope_state='verified'))
            # Prose may express the same depth as "толщиной 0, 20 м".
            # These literal anchors repair an already selected same-source
            # proposal; they do not select a construction or verify its scope.
            for match in re.finditer(r'\bтолщин[а-яё]*\s+(\d+(?:[.,]\s*\d+)?)\s*(мм|см|м)\b', text, re.I):
                start = max(text.rfind('\n', 0, match.start()), text.rfind('. ', 0, match.start())) + 1
                scope = text[start:match.end()].strip()
                if len(scope) <= 900:
                    result.append(dict(parameter='thickness', value=re.sub(r'\s+', '', match[1]).replace(',', '.'),
                        unit=match[2].lower(), source_id=source['id'], quote=match[0],
                        scope_quote=scope, scope_state='verified'))
        if source['kind'] == 'normative':
            for match in re.finditer(r'Измеритель\s*:?\s*(?:(\d+(?:[.,]\d+)?)\s*)?(м[23²³]?|шт\.?|т|кг|га)(?!\w)',text,re.I):
                result.append(dict(parameter='unit_scale',value=(match[1] or '1').replace(',','.'),unit=match[2],
                    source_id=source['id'],quote=match[0],scope_quote=match[0],scope_state='verified'))
    return result[:32]


def resolve_anchors(proposals, sources, items, *, work_scope='', height_cm=None, owner_unit=''):
    """Repair a citation only through a unique literal anchor in the SAME source.

    A missing or conflicting engineering scope is never upgraded by this step.
    """
    from workspace_models import NormativeParameterProposal
    candidates = parameter_candidates(sources)
    lookup = {s['id']:s for s in sources}
    for proposal in proposals:
        if proposal.operation != 'direct' or proposal.parameter not in {'thickness','unit_scale'}:
            continue
        source = lookup.get(proposal.source_id)
        if source is None:
            continue
        literal_valid = (unit_scale_quoted(proposal.value, proposal.unit, proposal.quote, source['text'])
            if proposal.parameter == 'unit_scale' else quoted(proposal.value, proposal.quote, source['text']))
        if literal_valid and proposal.scope_quote and proposal.scope_quote in source['text']:
            continue
        matches = [c for c in candidates if c['parameter'] == proposal.parameter and c['source_id'] == proposal.source_id
            and measure(c['value'],c['unit']) == measure(proposal.value,proposal.unit)
            and not construction_conflict(c['scope_quote'],work_scope,height_cm)]
        if len(matches) == 1:
            candidate = matches[0]
            proposal.value,proposal.unit = candidate['value'],candidate['unit']
            proposal.quote = candidate['quote']
            if not (proposal.scope_quote and clean(proposal.scope_quote) in clean(source['text'])
                    and quoted(proposal.value, proposal.quote, proposal.scope_quote)):
                proposal.scope_quote = candidate['scope_quote']
    if not any(p.parameter == 'unit_scale' for p in proposals):
        selected = {sid for i in items if 'normative:applicability' in i.claim_ids for sid in i.normative_source_ids}
        matching_units = [c for c in candidates if c['parameter'] == 'unit_scale' and c['source_id'] in selected
            and (value := measure(c['value'],c['unit'])) and (owner := measure('1',owner_unit)) and value[0] == owner[0]]
        values = {measure(c['value'],c['unit']) for c in matching_units}
        if matching_units and len(values) == 1:
            proposals.append(NormativeParameterProposal(**matching_units[0]))
    return proposals
