"""A norm-definition laying-pass limit cannot be closed by a machine capacity."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit_models import VorPosition
from normative_conditional_selection import apply_guard, feedback, repair_candidates
from normative_laying_width_scope import REASON, width_findings
from normative_parameters import clean,enrich

CASES=json.loads((Path(__file__).parent/'fixtures/laying-width-live.json').read_text())


def actual(index=26):
    row=next(c for c in CASES if c['index']==index)
    return VorPosition.model_validate(row['position']),deepcopy(row['review'])


def findings(position,result):
    return width_findings(position,result,{position.index:position},{},cards=result['parameter_cards'])


def bound_width(result,text='ширина укладки 5 м'):
    proposal=next(p for p in result['parameter_proposals'] if p['parameter']=='thickness')
    source=next(s for s in result['sources'] if s['id']==proposal['source_id'])
    old=proposal['scope_quote']
    proposal['scope_quote']=old+'; '+text
    assert clean(old) in clean(source['text'])
    source['text']=clean(source['text']).replace(clean(old),clean(proposal['scope_quote']))


@pytest.mark.parametrize('index',[26,74])
def test_actual_limited_base_requires_own_laying_pass_width(index):
    position,result=actual(index)
    original=deepcopy(result)
    found=findings(position,result)
    assert len(found)==1 and found[0]['reason']==REASON
    assert found[0]['selected_base_codes']==['27-06-031-01']
    assert 'ширина укладки до 6 м' in found[0]['condition_quote']
    assert found[0]['allowed_base_codes']==[]
    assert repair_candidates(position,result,{index:position},{})==[]
    assert any('Ширина дороги/полосы движения' in line for line in feedback(position,result,{index:position},{}))
    assert apply_guard(position,result,{index:position},{},cards=result['parameter_cards'])
    assert all(i['state']=='needs_facts' for i in result['items'] if 'normative:applicability' in i.get('claim_ids',[]))
    assert not any(i['state']=='possible_mismatch' for i in result['items'])
    assert result['parameter_cards']==original['parameter_cards']
    assert result['sources']==original['sources'] and result['parameter_proposals']==original['parameter_proposals']


@pytest.mark.parametrize('index',[37,59])
def test_other_norms_ignore_resource_maximum_width_and_machine_class(index):
    position,result=actual(index)
    assert findings(position,result)==[]
    # Even a plausible restriction on a resource is not a norm definition.
    result['sources'].append(dict(id='capacity',kind='normative',label='ГЭСН 27 · 27-06-029-01 · ресурсы',
        source_id='pdf',origin='native',text='Ресурсы нормы 27-06-029-01; максимальная ширина укладки до 5 м'))
    assert findings(position,result)==[]


@pytest.mark.parametrize('text',['ширина укладки 5 м','ширина укладки: 600 см','ширина укладки 6000 мм.'])
def test_only_explicit_width_attached_to_verified_own_pd_scope_can_discharge_guard(text):
    position,result=actual()
    bound_width(result,text)
    original=deepcopy(result)
    assert findings(position,result)==[]
    assert result==original  # No positive item/card/verdict is produced.


@pytest.mark.parametrize('text',['ширина проезжей части 5 м','ширина полосы движения 3 м',
    'максимальная ширина укладки машины 5 м','ширина укладки до 5 м','ширина укладки 5–6 м',
    'ширина укладки 5 м — 6 м','ширина укладки 5 м ± 1 м','ширина укладки 5 м и 7 м',
    'ширина укладки 7 м','ширина укладки 0 м','ширина укладки 5 м2',
    'ширина укладки около 5 м','для съездов ширина укладки 5 м',
    'Для съездов h=5 см; ширина укладки 5 м',
    'Другой участок: h=5 см; ширина укладки 5 м',
    'h=5 см; ширина укладки 5 м',
    'ширина укладки 5 м; ширина укладки 7 м'])
def test_other_scope_range_capacity_and_unverified_width_remain_unknown(text):
    position,result=actual()
    bound_width(result,text)
    assert findings(position,result)


@pytest.mark.parametrize('change',['same_page_elsewhere','different_source','unresolved_scope',
    'not_cited_project','unconfirmed_thickness','ocr','fabricated_scope','multiple_conditions'])
def test_literal_width_requires_verified_own_project_binding(change):
    position,result=actual()
    proposal=next(p for p in result['parameter_proposals'] if p['parameter']=='thickness')
    source=next(s for s in result['sources'] if s['id']==proposal['source_id'])
    if change=='same_page_elsewhere':
        source['text']+='\nДругой участок: ширина укладки 5 м.'
    elif change=='different_source':
        result['sources'].append(dict(source,id='other-pd',text='Ширина укладки 5 м'))
    else:
        bound_width(result)
        if change=='unresolved_scope':
            proposal['scope_state']='unresolved'
        elif change=='not_cited_project':
            result['sources'].append(dict(source,id='other-pd',text='Другая конструкция'))
            for item in result['items']:
                if 'normative:applicability' in item.get('claim_ids',[]):
                    item['project_source_ids']=['other-pd']
        elif change=='unconfirmed_thickness':
            next(c for c in result['parameter_cards'] if c['id']=='thickness')['state']='needs_facts'
        elif change=='ocr':
            source['origin']='ocr_unreviewed'
        elif change=='fabricated_scope':
            source['text']=source['text'].replace('; ширина укладки 5 м','')
        else:
            norm=next(s for s in result['sources'] if s['id']=='n2')
            norm['text']=norm['text'].replace('толщина слоя 4 см','ширина укладки до 8 м, толщина слоя 4 см')
    assert findings(position,result)


def test_negative_guard_preserves_existing_conflict_and_is_idempotent():
    position,result=actual()
    item=next(i for i in result['items'] if 'normative:applicability' in i.get('claim_ids',[]))
    item['state']='possible_mismatch'
    original=deepcopy(item)
    assert apply_guard(position,result,{position.index:position},{},cards=result['parameter_cards'])
    assert result['state']=='possible_mismatch' and item==original
    once=deepcopy(result)
    assert not apply_guard(position,result,{position.index:position},{},cards=result['parameter_cards'])
    assert result==once


def test_actual_numeric_cards_survive_full_enrich_while_applicability_stays_unknown():
    position,result=actual()
    checked=enrich(position,result,{position.index:position},{})
    assert checked['state']=='needs_facts'
    assert next(c for c in checked['parameter_cards'] if c['id']=='thickness')['state']=='confirmed'
    assert all(i['state']=='needs_facts' for i in checked['items'] if 'normative:applicability' in i.get('claim_ids',[]))
    assert enrich(position,checked,{position.index:position},{})==checked


def expert_fixture(position,result,maximum='6'):
    from analysis_replay import digest
    from normative_laying_width_scope import confirm_pass_width,supply_pass_width
    class Repository:
        supports_analysis_replay=True
        def __init__(self):self.artifacts={}
        def save_analysis_artifact(self,project,ident,payload):
            return deepcopy(self.artifacts.setdefault(digest(ident),dict(identity=ident,payload=payload))['payload'])
        def analysis_artifact(self,project,key):return deepcopy(self.artifacts.get(key))
    proposal=next(p for p in result['parameter_proposals'] if p['parameter']=='thickness')
    source=next(s for s in result['sources'] if s['id']==proposal['source_id'])
    version=source['version_id']
    record=dict(project_id='project',vor_version_id='vor',config=dict(pdf_versions={'pd':version},
        position_inputs=[position.model_dump(mode='json')]))
    ctx=SimpleNamespace(repository=Repository())
    receipt=confirm_pass_width(ctx,record,maximum,'user','Принятый способ работ этой ВОР','Да, проходами до 6 м')
    def add(s):
        result['sources'].append(s)
        return dict(source_id=s['id'])
    tools=SimpleNamespace(version=version,_add=add,preferred=[])
    supply_pass_width(ctx,record,position,tools)
    if tools.preferred:
        for item in result['items']:
            if 'normative:applicability' in item.get('claim_ids',[]):
                item['project_source_ids']+=tools.preferred
    return ctx,record,receipt,tools


def test_explicit_expert_width_discharges_only_width_guard():
    position,result=actual()
    original=deepcopy(result)
    expert_fixture(position,result)
    assert not findings(position,result)
    assert result['parameter_cards']==original['parameter_cards']
    assert result['parameter_proposals']==original['parameter_proposals']
    assert all(a['state']==b['state'] for a,b in zip(result['items'],original['items'],strict=True))
    assert result['sources'][:-1]==original['sources']


@pytest.mark.parametrize('change',['over_limit','uncited','wrong_work','wrong_version','wrong_stage',
    'no_author','no_quote','wrong_assertion','unconfirmed_thickness','unresolved_scope'])
def test_expert_width_requires_exact_work_pins_and_independent_pd_thickness(change):
    from analysis_replay import digest
    position,result=actual()
    expert_fixture(position,result,maximum='7' if change=='over_limit' else '6')
    source=result['sources'][-1]
    proof=source['expert_laying_width']
    if change=='uncited':
        for item in result['items']:
            item['project_source_ids']=[sid for sid in item['project_source_ids'] if sid!=source['id']]
    elif change=='wrong_work':
        proof['scope']='other'
    elif change=='wrong_version':
        source['version_id']='other'
    elif change=='wrong_stage':
        proof['identity']['stage']='other'
    elif change=='no_author':
        proof['fact']['actor']=''
    elif change=='no_quote':
        proof['fact']['user_quote']=''
    elif change=='wrong_assertion':
        proof['fact']['assertion']='as_built'
    elif change=='unconfirmed_thickness':
        next(c for c in result['parameter_cards'] if c['id']=='thickness')['state']='needs_facts'
    elif change=='unresolved_scope':
        next(p for p in result['parameter_proposals'] if p['parameter']=='thickness')['scope_state']='unresolved'
    if change not in ('uncited','over_limit','unconfirmed_thickness','unresolved_scope'):
        old=source['id']
        source['id']='expert:laying-width:'+digest(proof)[:32]
        for item in result['items']:
            item['project_source_ids']=[source['id'] if sid==old else sid for sid in item['project_source_ids']]
    assert findings(position,result)


@pytest.mark.parametrize('field',['project_id','vor_version_id','pdf_versions','work'])
def test_expert_fact_is_not_loaded_for_changed_inputs(field):
    from normative_laying_width_scope import supply_pass_width
    position,result=actual()
    ctx,record,_,tools=expert_fixture(position,result)
    if field=='pdf_versions':
        record['config'][field]={'pd':'different'}
    elif field=='work':
        position=position.model_copy(update={'comment':'changed'})
        record['config']['position_inputs']=[position.model_dump(mode='json')]
    else:
        record[field]='different'
    tools.preferred=[]
    supply_pass_width(ctx,record,position,tools)
    assert not tools.preferred
