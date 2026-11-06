"""Check a selected base against explicit, applicable normative conditions.

This never chooses a norm or promotes a conclusion. A literal condition can
invalidate a model's selection only after its material, layer and project
thickness have independently passed the ordinary evidence validators.
"""
import re

CODE = r'\d{2}\s*-\s*\d{2}\s*-\s*\d{3}\s*-\s*\d{2,3}'
TOPIC = 'Проверка условия выбора нормативного основания'
TRACE = 'conditional_norm_selection_v1'
CONDITION = re.compile(
    r'(?P<prefix>При\s+толщин[а-яё]*\s+[^.!?]{0,180}?)\s+'
    r'(?P<operator>не\s+менее|не\s+более|более|свыше|менее)\s+'
    r'(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мм|см|м)\b'
    r'(?P<action>\s+(?:(?:его|е[её]|их)\s+)?(?:(?:следует|необходимо)\s+)?'
    r'нормир[а-яё]*\s+в\s+(?:два|2)\s+слоя\s+по\s+норм[а-яё]*\s+'
    r'(?P<bases>'+CODE+r'(?:\s*(?:и|,)\s*'+CODE+r')*))'
    r'(?P<tail>[^.!?]{0,350})(?=[.!?]|$)',re.I)


def _readable(source):
    origin=source.get('origin','')
    return bool(origin and not origin.startswith('ocr')
        and origin not in {'uncertain_table','native_definition_ambiguous'})


def _code(source):
    codes={re.sub(r'\s+','',m[0]) for m in re.finditer(CODE,source.get('label',''))}
    return next(iter(codes)) if len(codes)==1 else None


def _role(text):
    roles=set(re.findall(r'\b(нижн|средн|верхн)[а-яё]*\s+сло[а-яё]*',text.casefold()))
    return next(iter(roles)) if len(roles)==1 else None


def condition_scope_matches(prefix,fact):
    """A physical PD layer must positively establish the clause's own branch.

    Two technological VOR operations do not make a middle/lower/upper PD layer
    a single-layer construction. This check applies even to a model-cited rule.
    """
    role=_role(prefix)
    single=bool(re.search(r'\bоднослойн[а-яё]*\b',prefix,re.I))
    if role and single:
        return False
    if role:
        return role==fact['role'] and not fact['single_layer']
    return single and fact['single_layer'] and fact['role'] is None


def _threshold_applies(match,fact):
    from normative_parameters import measure
    limit=measure(match['value'],match['unit'])
    if not limit or limit[0]!=fact['value'][0]:
        return False
    operator=re.sub(r'\s+',' ',match['operator'].casefold())
    value=fact['value'][1]
    return {'более':value>limit[1],'свыше':value>limit[1],
        'менее':value<limit[1],'не менее':value>=limit[1],'не более':value<=limit[1]}[operator]


def _project_thickness(position,result,cards):
    from normative_parameters import clean,measure,quoted,unit_quoted,construction_conflict
    from vor_material_links import _family
    card=next((c for c in cards if c['id']=='thickness' and c['state']=='confirmed'),None)
    family=_family(position.work_name)
    if not card:
        return None
    value=measure(card['found_value'],card['found_unit'])
    if not value:
        return None
    sources={s['id']:s for s in result['sources']}
    candidates=[]
    for proposal in result.get('parameter_proposals',[]):
        source=sources.get(proposal.get('source_id'))
        scope=proposal.get('scope_quote','')
        # A broad literal excerpt may include preceding layers of other
        # materials. Identify material only in the layer containing this
        # uniquely selected height; retain the full scope for all other guards.
        material_scope=scope
        quote=proposal.get('quote','')
        if quote and scope.count(quote)==1:
            at=scope.index(quote)
            starts=list(re.finditer(r'\b(?:верхн|нижн|средн|дополнительн)[а-яё]*\s+сло[йя]\b',scope[:at],re.I))
            if starts:
                material_scope=scope[starts[-1].start():at+len(quote)]
        verified_header=bool(source and source.get('origin')=='ocr_verified_header' and source.get('numeric_verified')
            and any(f.get('parameter')=='thickness' and measure(f.get('value'),f.get('unit',''))==value
                for f in source.get('verified_parameters',[])))
        project_family=_family(material_scope)
        if (proposal.get('parameter')!='thickness' or proposal.get('scope_state')!='verified'
                or not source or source['kind']!='project'
                or not (_readable(source) or verified_header) or not source.get('version_id')
                or not quoted(proposal['value'],proposal['quote'],scope)
                or clean(scope) not in clean(source['text']) or not unit_quoted(proposal['unit'],proposal['quote'])
                or not project_family or (family and project_family!=family)
                or construction_conflict(scope,position.work_name)):
            continue
        role=_role(scope)
        if _role(position.work_name) and role!=_role(position.work_name):
            continue
        candidate_value=measure(proposal['value'],proposal['unit'])
        if not candidate_value or candidate_value[0]!=value[0]:
            continue
        candidates.append(dict(source_id=source['id'],value=candidate_value,role=role,
            single_layer=bool(re.search(r'\bоднослойн[а-яё]*\b',scope,re.I)),family=project_family,
            quote=proposal['quote'],scope_quote=scope))
    if not candidates or len({(c['value'],c['role'],c['single_layer'],c['family']) for c in candidates})!=1:
        return None
    return next((c for c in candidates if c['value']==value and c['source_id'] in card.get('source_ids',[])),None)


def _named_in_work(source,position):
    """An explicit VOR reference remains required if the model omits its ID."""
    text=getattr(position,'comment','')
    clause=re.match(r'\s*(\d+(?:\.\d+){2,4})\b',source.get('text',''))
    family=re.search(r'\b(ГЭСН[мр]?)\s+(\d{1,2})\b',source.get('label',''),re.I)
    return bool(clause and family and re.search(r'(?<![\d.])'+re.escape(clause[1])+r'(?!\d|\.\d)',text)
        and re.search(re.escape(family[1])+r'\s+0?'+str(int(family[2]))+r'(?!\d)',text,re.I))


def _base_choices(selected,sources):
    """A literally referenced thickness adjustment is not a competing base."""
    from normative_preflight import adjustment_references
    def definition(source):
        code=_code(source)
        family=re.search(r'\b(?:ГЭСН|ФЕР|ТЕР)[а-яё]*\b',source.get('label',''),re.I)
        if not code or not family or not _readable(source) or not source.get('source_id'):
            return None
        return dict(code=code,table_code=code.rsplit('-',1)[0],source_id=source['source_id'],
            family=family[0].casefold(),text=source['text'])
    definitions=[item for source in sources.values() if (item:=definition(source))]
    bases=[]
    adjustments=[]
    for choice in selected:
        own=[item for item in definitions if item['code']==choice['code'] and item['source_id']==choice['pdf']]
        others=[item for item in definitions if any(other['code']==item['code'] and other['pdf']==item['source_id']
            and other['code']!=choice['code'] for other in selected)]
        if any(adjustment_references(item,base) for item in own for base in others):
            adjustments.append(choice)
        else:
            bases.append(choice)
    return bases,adjustments


def selection_findings(position,result,positions,relation,*,cards=None):
    from normative_parameters import build_cards,measure,unit_scale_quoted
    from vor_material_links import _family
    if result.get('pipeline_version',0)<3 or result.get('execution_state')=='technical_failure':
        return []
    if str(getattr(position,'position_type','')).strip().casefold()=='материал':
        lookup={s['id']:s for s in result.get('sources',[])}
        selected={sid for item in result.get('items',[]) if 'normative:applicability' in item.get('claim_ids',[])
            for sid in item.get('normative_source_ids',[])}
        if not any(_code(lookup[sid]) for sid in selected if sid in lookup):
            # This material has not selected a work-cost definition. A named
            # parent clause cannot impose that work's machine/layer choice on
            # an independent project quantity. Resource-rate proofs still
            # require their explicitly selected norm and all its conditions.
            return []
    owner=positions.get((relation.get('parent') or {}).get('index'),position)
    if cards is None:
        literal={**result,'local_work_basis':None,'quantity_composition':None}
        cards=build_cards(position,literal,positions,relation)
    from normative_laying_width_scope import width_findings
    findings=width_findings(position,result,positions,relation,cards=cards)
    fact=_project_thickness(owner,result,cards)
    independent_premise=False
    if not fact:
        # A final model paragraph can mix a literally verified thickness with
        # an uncertain child rate. That cannot make the applicable condition
        # disappear. Recheck only the negative guard's premise independently;
        # do not publish or replace any uncertain geometry card or claim.
        literal={**result,'local_work_basis':None,'quantity_composition':None,
            'items':[dict(item,claim_ids=[claim for claim in item.get('claim_ids',[]) if 'thickness' not in claim])
                for item in result.get('items',[])]}
        premise_cards=build_cards(position,literal,positions,relation)
        fact=_project_thickness(owner,result,premise_cards)
        independent_premise=bool(fact)
    if not fact:
        return findings
    from normative_layer_assembly import single_base_price_application
    price_proof=single_base_price_application(owner,result,positions,relation,cards=cards)
    sources={s['id']:s for s in result['sources']}
    applicability=[i for i in result.get('items',[]) if 'normative:applicability' in i.get('claim_ids',[])
        and i.get('topic')!=TOPIC]
    cited={sid for item in applicability for sid in item.get('normative_source_ids',[])}
    selected=[]
    for proposal in result.get('parameter_proposals',[]):
        source=sources.get(proposal.get('source_id'))
        # The model's scope assertion is not a prerequisite for a negative
        # condition check. A literal measure can name a cited selection even
        # when its applicability is unresolved. Otherwise omitting this field
        # suppresses the guard while the same measure card is already green.
        # The independently validated PD premise and literal source checks
        # below remain mandatory; this path never confirms applicability.
        if (proposal.get('parameter')=='unit_scale'
                and source and source['kind']=='normative' and _readable(source) and source.get('source_id')
                and _code(source) and unit_scale_quoted(proposal['value'],proposal['unit'],proposal['quote'],source['text'])):
            selected.append(dict(code=_code(source),source_id=source['id'],pdf=source['source_id']))
    app_codes={(_code(sources[sid]),sources[sid].get('source_id')) for sid in cited if sid in sources}
    selected,adjustments=_base_choices(selected,sources)
    for source in result['sources']:
        if (source['kind']!='normative' or not _readable(source) or not source.get('source_id')
                or source['id'] not in cited and not _named_in_work(source,owner)):
            continue
        # The introductory material scope belongs to this clause, not to an
        # unrelated table found during the initial search.
        first_condition=re.search(r'При\s+толщин',source['text'],re.I)
        if not first_condition or _family(source['text'][:first_condition.start()])!=fact['family']:
            continue
        scope_fact=(dict(fact,single_layer=True) if price_proof
            and price_proof['condition_source_id']==source['id'] else fact)
        for match in CONDITION.finditer(source['text']):
            if not condition_scope_matches(match['prefix'],scope_fact):
                continue
            limit=measure(match['value'],match['unit'])
            if not limit or limit[0]!=fact['value'][0]:
                continue
            operator=re.sub(r'\s+',' ',match['operator'].casefold())
            value=fact['value'][1]
            applies={'более':value>limit[1],'свыше':value>limit[1],
                'менее':value<limit[1],'не менее':value>=limit[1],'не более':value<=limit[1]}[operator]
            if not applies:
                continue
            bases=sorted({re.sub(r'\s+','',m[0]) for m in re.finditer(CODE,match['bases'])})
            tables={code.rsplit('-',1)[0] for code in bases}
            choices=[s for s in selected if s['pdf']==source['source_id'] and s['code'].rsplit('-',1)[0] in tables]
            codes=sorted({s['code'] for s in choices})
            bound=all((s['code'],s['pdf']) in app_codes for s in choices)
            # "By norms A and B" names separate required layers, not a menu.
            # This narrow guard cannot establish each layer's allocation.
            if len(bases)==1 and len(codes)==1 and bound and codes[0] in bases:
                continue
            if (price_proof and price_proof['condition_source_id']==source['id']
                    and price_proof['condition_quote']==match[0]
                    and set(price_proof['required_base_codes'])==set(bases)):
                continue
            reason=('layered_base_selection_unresolved' if len(bases)>1 else
                'base_outside_condition' if len(codes)==1 and bound else 'base_selection_ambiguous')
            findings.append(dict(kind=TRACE,reason=reason,condition_source_id=source['id'],
                condition_quote=match[0],allowed_base_codes=bases,selected_base_codes=codes,
                applicability_links_verified=bool(choices) and bound,
                selected_source_ids=sorted({s['source_id'] for s in choices}),project_source_id=fact['source_id'],
                project_quote=fact['quote'],project_scope_quote=fact['scope_quote'],layer_role=fact['role'],
                independent_literal_premise=independent_premise,
                adjustment_source_ids=sorted({s['source_id'] for s in adjustments})))
        # A named/cited multi-branch rule does not cover a different physical PD
        # role merely because its thickness threshold happens to be satisfied.
        # Keep the numerical facts; request the missing branch mapping instead
        # of suggesting a base from an inapplicable lower/upper/single branch.
        branches=[match for match in CONDITION.finditer(source['text'])
            if (_role(match['prefix']) or re.search(r'\bоднослойн',match['prefix'],re.I))
            and _threshold_applies(match,fact)]
        if not branches or any(condition_scope_matches(match['prefix'],scope_fact) for match in branches):
            continue
        tables={re.sub(r'\s+','',code[0]).rsplit('-',1)[0]
            for match in branches for code in re.finditer(CODE,match['bases'])}
        choices=[choice for choice in selected if choice['pdf']==source['source_id']
            and choice['code'].rsplit('-',1)[0] in tables and (choice['code'],choice['pdf']) in app_codes]
        if not choices:
            continue
        findings.append(dict(kind=TRACE,reason='construction_role_unresolved',condition_source_id=source['id'],
            condition_quote=source['text'],allowed_base_codes=[],
            selected_base_codes=sorted({choice['code'] for choice in choices}),
            selected_source_ids=sorted({choice['source_id'] for choice in choices}),
            project_source_id=fact['source_id'],project_quote=fact['quote'],
            project_scope_quote=fact['scope_quote'],layer_role=fact['role'],
            independent_literal_premise=independent_premise,
            adjustment_source_ids=sorted({s['source_id'] for s in adjustments})))
    return findings


def repair_candidates(position,result,positions,relation):
    """Literal alternatives after a proved violation, never applicability proof."""
    from normative_parameter_anchors import parameter_candidates
    from normative_preflight import adjustment_references
    sources={s['id']:s for s in result['sources']}
    output=[]
    for finding in selection_findings(position,result,positions,relation):
        condition=sources[finding['condition_source_id']]
        available=[s for s in sources.values() if s.get('kind')=='normative' and _readable(s)
            and s.get('source_id')==condition['source_id'] and 'ресурсы' not in s.get('label','')]
        bases=[]
        for source in available:
            code=_code(source)
            family=re.search(r'\b(?:ГЭСН|ФЕР|ТЕР)[а-яё]*\b',source.get('label',''),re.I)
            if not code or not family or code not in finding['allowed_base_codes']:
                continue
            measures=[p for p in parameter_candidates([source]) if p['parameter']=='unit_scale']
            if len(measures)!=1:
                continue
            base=dict(code=code,table_code=code.rsplit('-',1)[0],source_id=source['source_id'],
                family=family[0].casefold(),text=source['text'])
            adjustments=[]
            for other in available:
                other_code=_code(other)
                other_family=re.search(r'\b(?:ГЭСН|ФЕР|ТЕР)[а-яё]*\b',other.get('label',''),re.I)
                if not other_code or not other_family or other_code==code:
                    continue
                candidate=dict(code=other_code,table_code=other_code.rsplit('-',1)[0],source_id=other['source_id'],
                    family=other_family[0].casefold(),text=other['text'])
                if adjustment_references(candidate,base):
                    adjustments.append(dict(source_id=other['id'],code=other_code,text=other['text'][:1400]))
            measure={**measures[0],'scope_state':'unresolved'}
            bases.append(dict(source_id=source['id'],code=code,text=source['text'][:1400],
                literal_measure=measure,adjustments=adjustments[:3]))
        if bases:
            output.append(dict(condition_source_id=condition['id'],condition_quote=finding['condition_quote'],
                project_source_id=finding['project_source_id'],project_quote=finding['project_quote'],
                project_scope_quote=finding['project_scope_quote'],selected_source_ids=finding['selected_source_ids'],
                allowed_base_candidates=bases[:3],scope_verified=False,
                project_fact_confirmed=not finding['independent_literal_premise']))
    return output[:3]


def feedback(position,result,positions,relation):
    from normative_thickness_adjustment import feedback as adjustment_feedback
    from normative_laying_width_scope import REASON as WIDTH_REASON,explanation as width_explanation
    conditional=[(width_explanation(f) if f['reason']==WIDTH_REASON else
        'ПД: «'+f['project_scope_quote']+'». Источник '+f['condition_source_id']+
        ' содержит отдельные условия для других конструктивных слоёв. Принадлежность текущего слоя ПД '
        'к применимой ветви не установлена. Одного порога толщины и двух технологических операций ВОР недостаточно. '
        'Проектные размеры уже переданы: не запрашивай их повторно. Установи нормативное отношение '
        'между физическим слоем конструкции и операциями сметной нормы; при отсутствии основания '
        'это вопрос инженеру-сметчику, а не автоматически запрос проектировщику или ошибка ВОР. '
        'Сохрани доказанную геометрию; нормативная применимость остаётся needs_facts, это не ошибка ВОР.'
        if f['reason']=='construction_role_unresolved' else
        'Нормативное основание не согласовано с применимым условием источника '+f['condition_source_id']+
        ': «'+f['condition_quote']+'». Выбранный измеритель относится к '+(', '.join(f['selected_base_codes']) or 'неустановленной базе')+
        '; условие прямо задаёт базу '+', '.join(f['allowed_base_codes'])+
        ('. Условие содержит несколько базовых норм для отдельных слоёв; проверь основание и измеритель каждого слоя. '
            'До отдельного подтверждения этого распределения применимость остаётся needs_facts. '
            if len(f['allowed_base_codes'])>1 else
            '. Выбери одну применимую базовую норму и её буквальный измеритель; необходимые поправки проверь отдельно. ')+
        'conditional_repair_candidates содержит уже доступные буквальные определения и измерители допустимых баз, если они найдены; '
        'проверь их применимость и исправь выбор сам. Это вопрос нормативного выбора, а не доказанная ошибка ВОР.')
        for f in selection_findings(position,result,positions,relation)][:4]
    return conditional+adjustment_feedback(position,result,positions,relation)


def apply_guard(position,result,positions,relation,*,cards=None):
    """Demote unsupported applicability; preserve independently proved geometry."""
    from normative_parameters import build_cards,numeric_card_keys
    if cards is None:
        cards=build_cards(position,{**result,'local_work_basis':None,'quantity_composition':None},positions,relation)
    from normative_layer_assembly import single_base_price_application
    price_proof=single_base_price_application(position,result,positions,relation,cards=cards)
    result['evidence_trace']=[e for e in result.get('evidence_trace',[])
        if e.get('kind')!='single_base_price_application_v1']
    if price_proof:
        result['evidence_trace'].append(dict(kind='single_base_price_application_v1',**price_proof))
    from normative_thickness_adjustment import apply_guard as apply_adjustment_guard
    adjustment_changed=apply_adjustment_guard(position,result,positions,relation,cards=cards)
    from normative_operation_layers import apply_guard as apply_operation_guard
    operation_changed=apply_operation_guard(position,result,positions,relation,cards)
    findings=selection_findings(position,result,positions,relation,cards=cards)
    if not findings:
        return adjustment_changed or operation_changed
    from normative_laying_width_scope import REASON as WIDTH_REASON,explanation as width_explanation
    unresolved_role=any(f['reason'] in {'construction_role_unresolved',WIDTH_REASON} for f in findings)
    changed=False
    result['state']='possible_mismatch' if any(i['state']=='possible_mismatch'
        and (unresolved_role or 'normative:applicability' not in i.get('claim_ids',[])) for i in result.get('items',[])) else 'needs_facts'
    warning=('Ширина технологического прохода для выбранной нормы не подтверждена проектным основанием.'
        if any(f['reason']==WIDTH_REASON for f in findings) else
        'Принадлежность конструктивного слоя ПД к ветви условия техчасти не установлена; нормативное основание требует уточнения.'
        if unresolved_role else
        'Выбранная базовая норма не согласована с буквальным применимым условием техчасти; нормативное основание требует уточнения.')
    preserved=[]
    for item in result.get('items',[]):
        if 'normative:applicability' in item.get('claim_ids',[]) and item.get('topic')!=TOPIC:
            if unresolved_role and item['state']=='possible_mismatch':
                continue
            # A previous layer-cache item may combine applicability with its
            # independently proved geometry. Split only already-confirmed
            # numeric claims; never promote an uncertain model claim.
            numeric=[claim for claim in item['claim_ids'] if numeric_card_keys([claim])]
            keys=numeric_card_keys(numeric)
            selected=[card for card in cards if card['id'] in keys]
            ids={sid for card in selected for sid in card.get('source_ids',[])}
            if (item['state']=='confirmed' and keys and {c['id'] for c in selected}==keys
                    and all(c['state']=='confirmed' for c in selected) and ids
                    and ids<=set(item.get('project_source_ids',[]))):
                preserved.append(dict(topic=item['topic']+' · Числовое основание',state='confirmed',
                    required=item.get('required',True),claim_ids=numeric,project_source_ids=sorted(ids),
                    normative_source_ids=[],explanation='Ранее подтверждённые числовые основания ПД повторно проверены; '
                        'выбор применимой нормы рассматривается отдельно.'))
                item['claim_ids']=[claim for claim in item['claim_ids'] if claim not in numeric]
            if item['state']!='needs_facts':
                changed=True
            item['state']='needs_facts'
            if warning not in item['explanation']:
                item['explanation']+=' '+warning
    result['items'].extend(preserved)
    for finding in findings:
        item=dict(topic=TOPIC,state='needs_facts',required=True,claim_ids=['normative:applicability'],
            project_source_ids=[finding['project_source_id']],
            normative_source_ids=list(dict.fromkeys([finding['condition_source_id'],*finding['selected_source_ids']]))[:8],
            explanation=(width_explanation(finding)+' Условие: «'+finding['condition_quote']+'».'
                if finding['reason']==WIDTH_REASON else
                'ПД: «'+finding['project_scope_quote']+'». Принадлежность этого конструктивного слоя '
                'к отдельной ветви процитированного условия не установлена; совпадение толщины не доказывает применимость. '
                'Условие: «'+finding['condition_quote']+'». Это не замечание к ВОР.'
                if finding['reason']=='construction_role_unresolved' else
                'Условие: «'+finding['condition_quote']+'». '
                'База по структурированному измерителю: '+(', '.join(finding['selected_base_codes']) or 'не установлена однозначно')+
                '. Указанные условием базы: '+', '.join(finding['allowed_base_codes'])+'. '
                + ('В выводе о применимости нет проверенной ссылки на выбранное определение нормы. '
                   if finding['selected_base_codes'] and not finding.get('applicability_links_verified') else '')
                + 'Это не замечание к ВОР.'))
        if item not in result['items']:
            result['items'].append(item)
            changed=True
    result['evidence_trace']=[e for e in result.get('evidence_trace',[]) if e.get('kind')!=TRACE]+findings
    return changed
