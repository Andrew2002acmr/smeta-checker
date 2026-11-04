"""Keep a selected norm's explicit laying-width limit unresolved without PD proof.

Machine resource capacity and road/lane width are not technological laying-pass
width. This negative guard never chooses another norm or promotes a verdict.
"""
import re
from decimal import Decimal, InvalidOperation

from analysis_replay import digest, get, put

from normative_parameters import clean, measure
from normative_thickness_rules import definition_text


REASON='laying_width_unverified'
LIMIT=re.compile(r'\bширин[а-яё]*\s+укладк[а-яё]*\s+(?:до|не\s+более)\s+'
    r'(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мм|см|м)\b',re.I)
WIDTH=re.compile(r'\bширин[а-яё]*\s+укладк[а-яё]*\s*[:=]?\s*'
    r'(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мм|см|м)\b',re.I)

EXPERT_STAGE='expert_laying_pass_width_v1'


def _work_key(position):
    from audit_models import VorPosition
    return digest(VorPosition.model_validate(position).model_dump(mode='json'))


def _third_paver(position):
    text=position.get('work_name','')
    return bool(re.search(r'асфальтоукладчик',text,re.I)
        and re.search(r'3\s*[-–]?\s*(?:го\s+)?типоразмер',text,re.I))


def _expert_identity(record):
    config=record['config']
    return dict(schema=1,stage=EXPERT_STAGE,project_id=str(record['project_id']),
        vor_version_id=str(record['vor_version_id']),pdf_versions=config.get('pdf_versions') or {},
        work_keys=sorted(_work_key(p) for p in config.get('position_inputs',[]) if _third_paver(p)))


def _positive(value):
    try:
        number=Decimal(str(value))
        return number if number.is_finite() and 0<number<=1000 else None
    except InvalidOperation:
        return None


def confirm_pass_width(context,record,maximum_m,actor,reason,user_quote):
    """A human method basis, pinned to the current documents and exact works."""
    ident=_expert_identity(record)
    value=_positive(maximum_m)
    if (value is None or not ident['work_keys'] or not ident['pdf_versions']
            or not actor.strip() or not reason.strip() or not user_quote.strip()):
        raise ValueError('Explicit width confirmation requires pinned works, author and original answer')
    fact=dict(schema=1,provenance='explicit_user_confirmation',maximum_m=str(value),actor=actor,
        reason=reason,user_quote=user_quote,assertion='planned_paving_pass_upper_bound_not_as_built_measurement')
    saved=put(context,ident,fact)
    if saved!=fact:
        raise ValueError('Different width confirmation exists; preserve history and resolve explicitly')
    return dict(key=digest(ident),identity=ident,fact=saved)


def supply_pass_width(context,record,owner,tools):
    ident=_expert_identity(record)
    if _work_key(owner) not in ident['work_keys']:
        return
    fact=get(context,ident)
    if fact is None:
        return
    proof=dict(identity=ident,fact=fact,scope=_work_key(owner))
    source=dict(id='expert:laying-width:'+digest(proof)[:32],kind='project',page=None,
        label='Экспертное основание способа укладки',version_id=tools.version,
        origin='explicit_expert_method_basis',expert_laying_width=proof,
        text=('Отдельное экспертное основание для закреплённых работ этой ВОР: '
            f'ширина технологического прохода не более {fact["maximum_m"]} м. '
            f'Ответ пользователя: «{fact["user_quote"]}». {fact["reason"]} '
            'Это подтверждение принятого способа работ, не цитата ПД и не исполнительный замер.'))
    added=tools._add(source)
    if added.get('source_id'):
        tools.preferred.append(source['id'])


def _expert_width(source,owner,project_source,limit):
    proof=source.get('expert_laying_width') or {}
    ident=proof.get('identity') or {}
    fact=proof.get('fact') or {}
    value=_positive(fact.get('maximum_m'))
    return bool(source.get('origin')=='explicit_expert_method_basis'
        and source.get('id')=='expert:laying-width:'+digest(proof)[:32]
        and ident.get('stage')==EXPERT_STAGE and ident.get('schema')==1
        and ident.get('project_id') and ident.get('vor_version_id')
        and proof.get('scope')==_work_key(owner) and proof['scope'] in ident.get('work_keys',[])
        and project_source and source.get('version_id')==project_source.get('version_id')
        and source.get('version_id') in (ident.get('pdf_versions') or {}).values()
        and fact.get('provenance')=='explicit_user_confirmation'
        and fact.get('assertion')=='planned_paving_pass_upper_bound_not_as_built_measurement'
        and fact.get('actor') and fact.get('reason') and fact.get('user_quote')
        and value is not None and limit[0]=='length' and value<=limit[1])


def _own_width(fact,source,limit):
    """Accept only width attached to the already verified own thickness scope.

    No scan of other page paragraphs, model prose, machine specifications or
    arbitrary source numbers can discharge this engineering condition.
    """
    if not fact or not source or source.get('origin')!='native':
        return False
    scope=clean(fact['scope_quote'])
    own_quote=clean(fact['quote'])
    if not own_quote or scope.count(own_quote)!=1:
        return False  # Repeated geometry may introduce a different construction.
    matches=list(WIDTH.finditer(scope))
    if len(matches)!=1 or len(re.findall(r'\bширин[а-яё]*\s+укладк[а-яё]*',scope))!=1:
        return False
    width=matches[0]
    amount=measure(width['value'],width['unit'])
    if not amount or amount[0]!=limit[0] or not 0<amount[1]<=limit[1]:
        return False
    # The width must follow the literal own thickness immediately, rather than
    # being borrowed from a neighbouring section inside an overbroad quote.
    before=scope[:width.start()].rstrip()
    after=scope[width.end():]
    if not re.search(re.escape(own_quote)+r'\s*[,;.]\s*$',before):
        return False
    return bool(re.fullmatch(r'\s*[.;]?\s*',after))


def width_findings(position,result,positions,relation,*,cards):
    if result.get('pipeline_version',0)<3 or result.get('execution_state')=='technical_failure':
        return []
    from normative_conditional_selection import _project_thickness
    from normative_layer_conditions import _selected_base
    owner=positions.get((relation.get('parent') or {}).get('index'),position)
    applications=[i for i in result.get('items',[]) if 'normative:applicability' in i.get('claim_ids',[])]
    selected={sid for item in applications for sid in item.get('normative_source_ids',[])}
    base=_selected_base({**result,'parameter_cards':cards},selected)
    if not base:
        return []
    source,definition=base
    text=definition_text(definition['text'])
    limits=list(LIMIT.finditer(text))
    if not re.search(r'\bширин[а-яё]*\s+укладк[а-яё]*',text,re.I):
        return []  # Resource capacities/class labels are not definition limits.
    lookup={s['id']:s for s in result['sources']}
    project_ids={sid for item in applications for sid in item.get('project_source_ids',[])
        if sid in lookup and lookup[sid].get('kind')=='project'}
    if not project_ids:
        return []  # Ordinary citation guards already reject this answer.
    fact=_project_thickness(owner,result,cards)
    if (len(limits)==1 and len(re.findall(r'\bширин[а-яё]*\s+укладк[а-яё]*',text,re.I))==1
            and fact and fact['source_id'] in project_ids
            and (limit:=measure(limits[0]['value'],limits[0]['unit'])) is not None
            and (_own_width(fact,lookup.get(fact['source_id']),limit)
                or any(_expert_width(lookup[sid],owner,lookup.get(fact['source_id']),limit)
                    for sid in project_ids))):
        return []
    pid=fact['source_id'] if fact and fact['source_id'] in project_ids else sorted(project_ids)[0]
    return [dict(kind='conditional_norm_selection_v1',reason=REASON,
        condition_source_id=source['id'],condition_quote=text,
        allowed_base_codes=[],selected_base_codes=[definition['code']],selected_source_ids=[source['id']],
        project_source_id=pid,project_quote=fact['quote'] if fact else '',
        project_scope_quote=fact['scope_quote'] if fact else '',layer_role=fact['role'] if fact else None,
        independent_literal_premise=False,adjustment_source_ids=[])]


def explanation(finding):
    return ('В определении выбранной нормы '+', '.join(finding['selected_base_codes'])+
        ' прямо ограничена ширина укладки. Для своей конструкции не подтверждена ширина технологического прохода '
        'по проекту или отдельному закреплённому экспертному основанию. Ширина дороги/полосы движения и максимальная ширина машины '
        'этого не доказывают. Сохраните подтверждённые количества; применимость нормы требует уточнения. '
        'Это не доказанная ошибка ВОР.')
