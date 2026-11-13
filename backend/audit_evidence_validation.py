"""Graph proposal validators without aggregate verdict reducers.

Schemas and mechanical leaf validators moved intact from the legacy
monolithic checker. Aggregate status reducers are intentionally absent.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from audit_evidence_tools import EvidenceRequest
from normative_thickness_adjustment import AdjustmentProposal
from workspace_models import NormativeParameterProposal, NormativePositionCheckItem


class CheckedItem(NormativePositionCheckItem):
    topic: str = Field(max_length=150)
    explanation: str = Field(max_length=1600)
    project_source_ids: list[str] = Field(max_length=8)
    normative_source_ids: list[str] = Field(max_length=8)


class CheckAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=2500)
    items: list[CheckedItem] = Field(max_length=16)
    parameters: list[NormativeParameterProposal] = Field(default_factory=list, max_length=16)


class AgentAnswer(CheckAnswer):
    requests: list[EvidenceRequest] = Field(default_factory=list, max_length=4)
    adjustments: list[AdjustmentProposal] = Field(default_factory=list, max_length=4)




def validate_items(items, sources, known_claims=None, *, evidence_trace=None,matching_checks=(),replaying=False,scope_claims=None,position=None,atomic=False,parameters=()):
    if atomic:
        from normative_fact_contract import atomic_items
        items = atomic_items(items, evidence_trace)
    separated=[]
    for item in items:
        if 'workbook:formula' in item.claim_ids and len(item.claim_ids)>1:
            # Arithmetic is proved by the server, while the remaining claims
            # retain their original state and source/scope requirements.
            separated.extend([item.model_copy(update={'claim_ids':[c for c in item.claim_ids if c!='workbook:formula']}),
                item.model_copy(update={'claim_ids':['workbook:formula']})])
        else:
            separated.append(item)
    project_ids = {s["id"] for s in sources if s["kind"] == "project"}
    norm_ids = {s["id"] for s in sources if s["kind"] == "normative"}
    output = []
    # A public source (PDF) identifier is only an alias when it identifies one
    # and only one supplied excerpt. Never repair a fabricated reference by page
    # or by matching the expected VOR number.
    aliases = {}
    for source in sources:
        for alias in (source['id'], source.get('source_id')):
            if alias:
                aliases.setdefault(alias,set()).add(source['id'])
    from normative_advisory_scope import partition
    scoped = [part for item in separated for part in partition(item, position, matching_checks,
        known_claims if scope_claims is None else scope_claims)]
    for item, advisory in scoped:
        row = item.model_dump()
        for key in ('project_source_ids','normative_source_ids'):
            row[key] = list(dict.fromkeys(next(iter(aliases[s])) if len(aliases.get(s,()))==1 else s for s in row[key]))
        row['required'] = True  # Only local validation may scope a warning away.
        if known_claims is not None:
            invalid_claims = set(row['claim_ids']) - set(known_claims)
            row['claim_ids'] = [c for c in row['claim_ids'] if c in known_claims]
            if invalid_claims:
                row['state'] = 'needs_facts'
            if atomic and not row['claim_ids']:
                if evidence_trace is not None:
                    evidence_trace.append(dict(kind='unaddressed_model_claim', original=item.model_dump()))
                continue
        if row['claim_ids'] and all(c.startswith('related:') for c in row['claim_ids']):
            row['required'] = False
        valid = set(row["project_source_ids"]) <= project_ids and set(row["normative_source_ids"]) <= norm_ids
        row["project_source_ids"] = [i for i in row["project_source_ids"] if i in project_ids]
        row["normative_source_ids"] = [i for i in row["normative_source_ids"] if i in norm_ids]
        from normative_verdict import source_supported,absence_claim,literal_project_designations
        uncertain_norms = {s['id'] for s in sources if s['kind'] == 'normative'
                           and str(s.get('origin', '')).startswith('ocr_unreviewed')}
        uncertain_inline = {s['id'] for s in sources if s.get('origin') == 'ocr_inline_unreviewed'}
        from normative_source_roles import unread_visual_source, UNREAD_VISUAL_MESSAGE
        unread_visual = {s['id'] for s in sources if unread_visual_source(s)}
        if unread_visual.intersection(row['project_source_ids'] + row['normative_source_ids']):
            row['state'] = 'needs_facts'
            if UNREAD_VISUAL_MESSAGE not in row['explanation']:
                row['explanation'] += ' ' + UNREAD_VISUAL_MESSAGE
        if uncertain_inline.intersection(row['project_source_ids']):
            row['state'] = 'needs_facts'
            row['explanation'] += ' В источнике есть непроверенные растровые символы; нужна сверка указанных областей.'
        if uncertain_norms.intersection(row['normative_source_ids']):
            row['state'] = 'needs_facts'
            warning = 'Нормативное основание получено непроверенным OCR; требуется сверка с оригиналом.'
            if warning not in row['explanation']:
                row['explanation'] += ' ' + warning
        ambiguous_definitions = {s['id'] for s in sources if s['kind']=='normative'
            and s.get('origin')=='native_definition_ambiguous'}
        if ('normative:applicability' in row['claim_ids']
                and ambiguous_definitions.intersection(row['normative_source_ids'])):
            row['state']='needs_facts'
            warning='Область определения этой нормы извлечена неоднозначно; применимость требует однозначного нормативного основания.'
            if warning not in row['explanation']:
                row['explanation']+=' '+warning
        from normative_source_roles import classification_only_basis, MESSAGE
        if classification_only_basis(row, sources):
            row['state'] = 'needs_facts'
            if MESSAGE not in row['explanation']:
                row['explanation'] += ' ' + MESSAGE
        if absence_claim(row):
            row['state']='needs_facts'
            row['explanation']+=' Отсутствие обозначения в найденном фрагменте не доказывает несовместимость; требуется сопоставление одного свойства и шкалы.'
        from normative_workbook_rate import project_consumption_supported
        if (not valid or not source_supported(row)
                or not project_consumption_supported(row,sources,position=position,parameters=parameters,
                    items=[i.model_dump() for i in items])
                or not literal_project_designations(row,sources,position=position)):
            row["state"] = "needs_facts"
            warning = " Не хватает достоверных ссылок для этого аспекта проверки: нужна сверка."
            if warning.strip() not in row["explanation"]:
                row["explanation"] += warning
        from normative_item_scope import apply_scope
        apply_scope(item.model_dump(),row,known_claims if scope_claims is None else scope_claims,
            matching_checks,evidence_trace,replaying=replaying)
        if advisory:
            row['required'] = False
        start = len(output)
        related = [claim for claim in row['claim_ids'] if claim.startswith('related:')]
        own = [claim for claim in row['claim_ids'] if not claim.startswith('related:')]
        if related and own:
            # A mixed paragraph cannot establish which owner its conclusion
            # concerns. Preserve both obligations without making the child's
            # rate mandatory for the work or copying a positive/red verdict.
            split = {**row,'state':'needs_facts'}
            split['explanation'] += ' Вывод объединяет работу и дочерний материал; основания этих аспектов проверяются отдельно.'
            output.extend([{**split,'claim_ids':own,'required':True},
                           {**split,'claim_ids':related,'required':False}])
        else:
            if evidence_trace is not None:
                from normative_quantity_bridge import record_source_omission
                record_source_omission(item.model_dump(),row,sources,evidence_trace)
            output.append(row)
        from normative_claim_migration import record_condition_conflict
        for validated in output[start:]:
            record_condition_conflict(item.model_dump(),validated,position,evidence_trace)
    return output



def normalize_rounded_sum(proposal, computed, source, events):
    """Keep the exact verified sum when the model supplied its decimal rounding."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    from normative_parameters import number, quoted

    if proposal.parameter != 'quantity' or proposal.source_id != computed['source_id']:
        return
    displayed, exact = number(proposal.value), number(computed['value'])
    if displayed is None or exact is None or displayed == exact:
        return
    exponent = displayed.as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= 0:
        return
    try:
        if exact.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_HALF_UP) != displayed:
            return
    except InvalidOperation:
        return
    if not quoted(computed['value'], proposal.quote, source['text']):
        return
    events.append(dict(kind='verified_sum_rounding', original_value=proposal.value,
                       exact_value=computed['value'], source_id=proposal.source_id))
    proposal.value = computed['value']


def bind_explicit_sum_sources(answer,sources,tools):
    """Bind a missing calculated handle to the model's exact chosen operands.

    No operand selection, VOR substitution or scope upgrade. A fabricated
    supplied handle, wrong value, unreadable cell or incompatible unit is not
    repaired. The original semantic assertion stays available in the trace.
    """
    from normative_parameters import number
    for proposal in answer.parameters:
        if proposal.operation!='sum' or proposal.source_id:
            continue
        computed=tools.execute(EvidenceRequest(tool='sum_cells',
            operand_ids=proposal.operand_source_ids,unit=proposal.unit))
        if 'error' in computed or number(proposal.value)!=number(computed['value']):
            continue
        source=next(s for s in sources if s['id']==computed['source_id'])
        if len(source['text'])>900:
            continue
        original=proposal.model_dump()
        equation=source['text'].split('\n')[-1].partition(' Ячейки:')[0].rstrip('.')
        proposal.source_id=source['id']
        proposal.quote=equation
        proposal.scope_quote=source['text']
        tools.events.append(dict(kind='explicit_sum_source_binding_v1',
            original_proposal=original,replacement_proposal=proposal.model_dump()))



def answer_feedback(answer, sources, tools, *, owner_unit='', work_scope='', height_cm=None, section_context='', conditional_feedback=None, claim_definitions=(),quantity_work_scope=None,position=None,matching_checks=(),known_claims=None,project_quantity_contract=None):
    """Return mechanical evidence errors while the agent still has a round.

    No expected VOR quantity is supplied here. This checks selected source IDs,
    literal quotes and actual cells, never fabricates engineering applicability.
    """
    from normative_parameters import number,unit_scale_quoted,measure,clean,construction_conflict
    from normative_verdict import source_supported,literal_project_designations
    from normative_parameter_feedback import literal_parameter_issues
    from normative_source_roles import classification_only_basis, classification_source, MESSAGE
    bind_explicit_sum_sources(answer,sources,tools)
    canonicalize_cell_parameters(answer,sources,work_scope=work_scope,height_cm=height_cm,section_context=section_context)
    from normative_parameter_anchors import resolve_anchors
    from normative_workbook_rate import quantity_contract,project_consumption_supported
    role_contract=(project_quantity_contract if project_quantity_contract is not None
        else quantity_contract(position) if position is not None else None)
    if role_contract and not role_contract.get('project_input'):
        role_contract=None  # Subject metadata must not introduce a numeric input role.
    answer.parameters = resolve_anchors(answer.parameters,sources,answer.items,
        work_scope=work_scope,height_cm=height_cm,owner_unit=owner_unit,evidence_trace=tools.events,
        calculation_input_unit=role_contract['project_input']['unit'] if role_contract else '')
    # The model chooses direct versus summed scope. Feedback may reject a
    # partial reading, but must not replace it with a verified local decision.
    lookup={s['id']:s for s in sources}
    issues=[]
    quantities = [p for p in answer.parameters if p.parameter == 'quantity']
    measured=[m for p in quantities if (m:=measure(p.value,p.unit)) is not None]
    roles={m[0] for m in measured} if role_contract else {'quantity'}
    ambiguous=any(len({m for m in measured if not role_contract or m[0]==role})>1 for role in roles)
    if role_contract and position is not None:
        allowed={m[0] for unit in (position.unit,role_contract['project_input']['unit']) if (m:=measure('1',unit))}
        if any(m[0] not in allowed for m in measured):
            issues.append('Параметр quantity имеет размерность, отличную от собственного результата '
                'и явно заданной площади-основания формулы. Установи назначение числа по источнику.')
    if ambiguous:
        issues.append('Для quantity возвращено несколько разных величин. Сам установи охват позиции по ПД: '
            'выбери одно основание либо запроси sum_cells и верни operation=sum с точными operand_source_ids, '
            'если источники доказывают, что эти части составляют общий объём. Код не выбирает и не складывает их вместо тебя.')
    evidence=dict(getattr(tools,'parameter_evidence',{}))
    tools.invalid_project_parameter_kinds=set()
    definitions={d['claim_id']:d for d in claim_definitions}
    from normative_advisory_scope import partition
    scoped = [part for item in answer.items for part in partition(item, position, matching_checks, known_claims)]
    for item, advisory in scoped:
        unknown=(set(item.project_source_ids)|set(item.normative_source_ids))-lookup.keys()
        if unknown:
            issues.append('Неизвестные источники: '+', '.join(sorted(unknown)))
        if any(c.startswith('related:') for c in item.claim_ids) and any(not c.startswith('related:') for c in item.claim_ids):
            issues.append('Раздели основной аспект и related:* на отдельные items; коэффициент дочерней строки не относится к применимости родительской работы.')
        if not advisory and item.state in {'confirmed','possible_mismatch'} and item.claim_ids != ['workbook:formula']:
            correct_kinds=(all(lookup.get(s,{}).get('kind')=='project' for s in item.project_source_ids)
                and all(lookup.get(s,{}).get('kind')=='normative' for s in item.normative_source_ids))
            if (not correct_kinds or not source_supported(item.model_dump())
                    or not project_consumption_supported(item.model_dump(),sources,position=position,
                        parameters=[p.model_dump() for p in answer.parameters],items=[i.model_dump() for i in answer.items])
                    or not literal_project_designations(item.model_dump(),sources,
                        position=position,definitions=claim_definitions)):
                issues.append('Аспект '+', '.join(item.claim_ids)+': подтверждённый вывод требует ссылок правильного типа. '
                    'Наличие/геометрию/количество свяжи с ПД; для способа работ, условия комментария и нормативной применимости '
                    'укажи ПД, задающий эту конструкцию, и применимый норматив. ПД не обязан повторять марку машины из нормы. '
                    'Используй только существующие прямые основания; если связь не доказана, оставь needs_facts, не добавляй случайную ссылку.')
        if not advisory and classification_only_basis(item.model_dump(), sources):
            issues.append('Аспект '+', '.join(item.claim_ids)+': '+MESSAGE)
        for claim in item.claim_ids:
            definition=definitions.get(claim)
            expected=(definition or {}).get('expected_text','')
            if not expected or item.state=='confirmed':
                continue
            equipment=(claim.startswith('method:comment:equipment:')
                and re.search(r'марк|эквивалент|модел',item.explanation,re.I))
            child_rate=(claim.startswith('basis:comment:')
                and re.search(r'коэффициент|Купл|расход[^.!?]{0,70}дочер',item.explanation,re.I)
                and not re.search(r'коэффициент|Купл|расход',expected,re.I))
            if equipment or child_rate:
                issues.append('Аспект '+claim+' имеет точное требование «'+expected+'». '
                    'Проверь именно его по ПД и нормативу, а не дополнительную марку машины или коэффициент дочерней строки. '
                    'Неизвестный расход дочернего материала оставь отдельным related:*; неизвестная марка не заменяет проверку '
                    'указанной функции/класса. Если само требование не подтверждается, сохрани needs_facts с причиной по этому требованию.')
    for proposal in answer.parameters:
        source=lookup.get(proposal.source_id)
        if (proposal.parameter in {'unit_scale', 'coefficient'} and source is not None
                and classification_source(source)):
            evidence.pop(proposal.parameter, None)
            issues.append('Параметр '+proposal.parameter+': '+MESSAGE)
            continue
        if proposal.parameter=='quantity':
            from normative_paired_scope import scope_issue
            scope_problem=scope_issue(source or{},work_scope)
            if proposal.scope_state=='verified'and scope_problem:
                tools.invalid_project_parameter_kinds.add('quantity')
                evidence.pop('quantity',None)
                issues.append(scope_problem)
                continue
            from normative_quantity_scope import partial_side_issue,unresolved_layer_issue
            partial=partial_side_issue(proposal.model_dump(),sources,
                work_scope if quantity_work_scope is None else quantity_work_scope,section_context)
            if partial:
                tools.invalid_project_parameter_kinds.add('quantity')
                evidence.pop('quantity',None)
                issues.append(partial['reason'])
                continue
            layer_issue=unresolved_layer_issue(proposal.model_dump(),source,
                work_scope if quantity_work_scope is None else quantity_work_scope,height_cm)
            if layer_issue:
                tools.invalid_project_parameter_kinds.add('quantity')
                evidence.pop('quantity',None)
                issues.append(layer_issue)
                continue
        if proposal.parameter in {'quantity','thickness'} and source is not None:
            scope=(source['text'] if ':cell:' in source['id'] or source.get('origin')=='calculated_from_cells'
                else proposal.scope_quote)
            if construction_conflict(scope,work_scope,height_cm):
                if proposal.scope_state=='verified':
                    tools.invalid_project_parameter_kinds.add(proposal.parameter)
                issues.append('Параметр '+proposal.parameter+': выбранная цитата относится к другому материалу или полной проектной толщине. '
                    'Проверь literal parameter_candidates и заголовки той же конструкции; для технологических слоёв сопоставляй '
                    'полную толщину vor_layer_group, а не толщину одной операции. Не подставляй ожидаемое число ВОР '
                    'и не переноси цитату соседнего слоя; если нужный источник не установлен, scope_state=unresolved.')
                continue
        if proposal.operation=='sum':
            result=tools.execute(EvidenceRequest(tool='sum_cells',operand_ids=proposal.operand_source_ids,unit=proposal.unit))
            if 'error' in result:
                issues.append('Сумма: '+result['error']+'. Проверь operand_source_ids в quantity_candidates; не используй пустые или соседние ячейки.')
                continue
            source=next((source for source in tools.sources if source['id']==result['source_id']),None)
            if source is not None:
                normalize_rounded_sum(proposal, result, source, tools.events)
            if number(proposal.value)!=number(result['value']):
                issues.append('Параметр '+proposal.parameter+': value не равно точной сумме заявленных operand_source_ids. '
                    'Используй точное значение из sum_cells / quantity_parameter_candidates, без округления до ВОР; '
                    'сравнение и допустимое округление выполняются сервером позже. Это не доказательство ошибки ВОР.')
                continue
            if proposal.source_id != result['source_id']:
                issues.append('Параметр '+proposal.parameter+': source_id не относится к сумме заявленных operand_source_ids. '
                    'Скопируй source_id результата sum_cells, не ссылку на отдельную ячейку или страницу.')
                continue
            errors=literal_parameter_issues(proposal,source)
            issues.extend(errors)
            if not errors and proposal.scope_state=='verified':
                evidence[proposal.parameter]=proposal.model_dump()
        else:
            source=lookup.get(proposal.source_id)
            if proposal.parameter == 'unit_scale':
                if source is None or source.get('kind') != 'normative':
                    issues.append('Параметр unit_scale: нужен нормативный источник выбранной нормы работы, а не ПД. Дочитай её read_source и скопируй строку «Измеритель».')
                    continue
                if ('измеритель' not in clean(proposal.quote)
                        or not unit_scale_quoted(proposal.value, proposal.unit, proposal.quote, source['text'])):
                    issues.append('Параметр unit_scale: число и единицу бери из буквальной строки «Измеритель» нормы работы, не из количества или единицы ресурса. Дочитай read_source указанного норматива.')
                    continue
                scale,owner=measure(proposal.value,proposal.unit),measure('1',owner_unit)
                dimensions={owner[0]} if owner else set()
                if role_contract and (input_measure:=measure('1',role_contract['project_input']['unit'])):
                    dimensions.add(input_measure[0])
                if scale and dimensions and scale[0] not in dimensions:
                    issues.append('Параметр unit_scale: размерность измерителя не совпадает с normative_work_owner.unit '
                        'или типизированным project_quantity_contract.project_input.unit. Установи выбранную норму '
                        'и роль её измерителя. Для независимого количества материала или площади input:area '
                        'unit_scale не обязателен; если расход взят из рабочей нормы, её условия и пересчёт обязательны.')
                    continue
                if proposal.scope_state=='verified':
                    evidence[proposal.parameter]=proposal.model_dump()
            else:
                errors=literal_parameter_issues(proposal,source)
                issues.extend(errors)
                if errors and proposal.parameter in {'quantity','thickness'} and proposal.scope_state=='verified':
                    tools.invalid_project_parameter_kinds.add(proposal.parameter)
                if not errors and source is not None and proposal.scope_state=='verified':
                    evidence[proposal.parameter]=proposal.model_dump()
    if conditional_feedback is not None:
        issues.extend(conditional_feedback(answer))
    tools.parameter_evidence=evidence
    return list(dict.fromkeys(issues))[:8]



def canonicalize_cell_parameters(answer,sources,*,work_scope='',height_cm=None,section_context=''):
    """Repair formatting in the exact verified cell or certified operand sum.

    Never search other cells, borrow the VOR value or promote unresolved scope.
    Every non-ellipsis scope fragment must already occur in that same source.
    """
    from normative_parameters import clean,measure,number,construction_conflict
    from vor_material_links import _family
    from normative_retrieval import terms
    # Subsite/side/axis ownership needs its own semantic proof. This narrow
    # formatting repair is not the place to infer such a physical partition.
    if not work_scope or re.search(r'\b(?:слева|справа|пк\s*\d)|\b(?:лев|прав)\w*\s+(?:ос|сторон)',
            work_scope+' '+section_context,re.I):
        return
    lookup={source['id']:source for source in sources}
    for proposal in answer.parameters:
        source=lookup.get(proposal.source_id)
        if (proposal.parameter == 'quantity' and proposal.operation == 'sum' and proposal.scope_state == 'verified'
                and source and source.get('kind') == 'project' and source.get('origin') == 'calculated_from_cells'
                and source.get('numeric_verified') and source.get('unit_verified')
                and not construction_conflict(source['text'],work_scope,height_cm)):
            from normative_parent_basis import _proposal_dependencies
            text = source['text']
            cells = [lookup[sid] for sid in proposal.operand_source_ids if sid in lookup]
            source_rects = sorted(tuple(rect) for rect in source.get('rects', []))
            cell_rects = sorted(tuple(rect) for cell in cells for rect in cell.get('rects', []))
            fragments = [clean(piece) for piece in re.split(r'\.{3,}|…', proposal.scope_quote or "") if clean(piece)]
            clean_text = clean(text)
            clean_unit = clean(str(proposal.unit))
            # Repair only when the draft scope carries no contradictory unit
            # or site: fragments mentioning another unit/site block repair,
            # while an empty or abbreviated draft is replaced wholesale.
            contradictory = False
            for fragment in fragments:
                if not (("т" in fragment.split() or "участ" in fragment)):
                    continue
                normalized = re.sub(r'(\b(?:площадь|объем|длина)\s*,)\s*'
                    + re.escape(clean_unit) + r'(?![\w/])', r'\1', fragment)
                if fragment not in clean_text and normalized not in clean_text:
                    contradictory = True
                    break
            if (len(text) <= 900 and _proposal_dependencies(proposal.model_dump(), lookup)
                    and source_rects and source_rects == cell_rects
                    and all(clean(re.sub(r'\n(?:Всего|Итого|Total)\s*:?\s*$', '',
                        cell['text'].rsplit('\n', 1)[0], flags=re.I)) in clean(text) for cell in cells)
                    and number(proposal.value) is not None
                    and number(text.rsplit('=', 1)[-1].split()[0] if '=' in text else '') == number(proposal.value)
                    and not contradictory):
                # Operands, geometry, and the value are all verified against
                # this exact sum source. Replace any shortened quote/scope
                # draft with the literal equation and source text; the model
                # abbreviates long citations and cannot guess them exactly.
                equation = text.rsplit('\n', 1)[-1].partition(' Ячейки:')[0].rstrip('.')
                proposal.quote = equation[:900]
                proposal.scope_quote = text
            continue
        if (proposal.parameter!='quantity' or proposal.operation!='direct' or proposal.scope_state!='verified'
                or not source or source.get('kind')!='project' or ':cell:' not in source['id']
                or not source.get('numeric_verified') or not source.get('unit_verified')
                or source.get('origin')=='uncertain_table'
                or proposal.operand_source_ids not in ([],[proposal.source_id])):
            continue
        text=source.get('text','')
        literal=text.rsplit('\n',1)[-1].strip()
        source_measure=measure('1',source.get('unit',''))
        if (number(literal) is None or number(literal)!=number(proposal.value) or not source_measure
                or measure('1',proposal.unit)!=source_measure
                or construction_conflict(text,work_scope,height_cm)):
            continue
        wanted_family,found_family=_family(work_scope),_family(text)
        if wanted_family and wanted_family!=found_family:
            continue
        section_terms=terms(section_context)-terms('Раздел')
        source_terms=terms(text)
        if any(not any(a==b or min(len(a),len(b))>=4 and (a.startswith(b) or b.startswith(a))
                for b in source_terms) for a in section_terms):
            continue
        fragments=[clean(piece) for piece in re.split(r'\.{3,}|…',proposal.scope_quote) if clean(piece)]
        if (not fragments or not all(piece in clean(text) for piece in fragments)
                or not any(len(piece)>=15 and re.search(r'[а-яa-z]{3,}',piece) for piece in fragments)
                or len(text)>900):
            continue
        # The source is a single physical cell with its original header chain.
        # Use that chain as the literal quotation, preserving the model's scope
        # state; downstream material/layer/section validation remains active.
        proposal.quote=text
        proposal.scope_quote=text
        proposal.operand_source_ids=[]



def validated_items(answer: CheckAnswer, sources: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    """Validate item citations/scopes without assigning an aggregate verdict."""
    return validate_items(answer.items, sources, **kwargs)


__all__ = [
    "AgentAnswer",
    "CheckAnswer",
    "CheckedItem",
    "answer_feedback",
    "bind_explicit_sum_sources",
    "canonicalize_cell_parameters",
    "normalize_rounded_sum",
    "validate_items",
    "validated_items",
]
