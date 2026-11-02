"""Keep a selected norm's explicit laying-width limit unresolved without PD proof.

Machine resource capacity and road/lane width are not technological laying-pass
width. This negative guard never chooses another norm or promotes a verdict.
"""
import re

from normative_parameters import clean, measure
from normative_thickness_rules import definition_text


REASON='laying_width_unverified'
LIMIT=re.compile(r'\bширин[а-яё]*\s+укладк[а-яё]*\s+(?:до|не\s+более)\s+'
    r'(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мм|см|м)\b',re.I)
WIDTH=re.compile(r'\bширин[а-яё]*\s+укладк[а-яё]*\s*[:=]?\s*'
    r'(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мм|см|м)\b',re.I)


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
            and _own_width(fact,lookup.get(fact['source_id']),limit)):
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
        'по буквальному проектному основанию. Ширина дороги/полосы движения и максимальная ширина машины '
        'этого не доказывают. Сохраните подтверждённые количества; применимость нормы требует уточнения. '
        'Это не доказанная ошибка ВОР.')
