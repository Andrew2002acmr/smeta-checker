from contextlib import contextmanager
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from normative_resource_recovery import hydrate


class KB:
    identity={'sha256':'immutable','release_id':'test'}

    def __init__(self,code='27-06-031-01'):
        self.code=code
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.execute('create table norms (id text,source_id text,code text)')
        self.db.execute("insert into norms values ('norm','pdf',?)",(code,))
        self.reads=0

    @contextmanager
    def connect(self):
        yield self.db

    def entity(self,kind,norm_id):
        self.reads+=1
        return dict(id=norm_id,code=self.code,status='native',resources=[
            dict(name='Смеси асфальтобетонные',quantity_kind='project')])

    def checks(self,item,facts):
        return [dict(state='unknown',label='Условие')],['Уточнить условие']

    def rules(self,item,facts):
        return []


def formatted(kb,item):
    return dict(id=item['id'],code=item['code'],entity_kind='norm',designation='ГЭСН 27',
        unit_raw='1000 м2',extraction_status='native',checks=item['checks'],rules=item['rules'],resources=[
            dict(code='material',name='Смеси асфальтобетонные',unit='т',quantity_kind='project',quantity=None,
                raw_quantity='П',status='native',citations=[dict(source_id='pdf',page=2)])])


def review() -> dict[str,Any]:
    return dict(sources=[dict(id='n',kind='normative',label='ГЭСН 27 · 27-06-031-01',
        origin='native',source_id='pdf',text='27-06-031-01 Измеритель: 1000 м2',page=1)],
        items=[dict(state='needs_facts',claim_ids=['normative:applicability'],normative_source_ids=['n'])])


def test_recovery_keeps_pinned_project_rate_and_does_not_approve_conditions():
    kb=KB()
    result=review()
    original=deepcopy(result)
    with patch('normative_service.candidate',side_effect=formatted):
        hydrate(kb,result)
        hydrate(kb,result)
    assert result['items']==original['items']
    assert len(result['sources'])==2 and 'П' in result['sources'][-1]['text']
    assert kb.reads==1
    event=next(e for e in result['evidence_trace'] if e['kind']=='normative_resource_consumption_candidates')
    assert event['checks'][0]['state']=='unknown'
    assert event['resources'][0]['quantity'] is None
    assert event['norm_source_id']=='n' and event['recovery_prefix'].startswith('recovered:')
    assert event['resources'][0]['source_id'].startswith(event['recovery_prefix']+':resources:')
    assert len([e for e in result['evidence_trace'] if e['kind']=='normative_resource_consumption_candidates'])==1
    assert not result['evidence_trace'][-1]['applicability_confirmed']


def test_other_pdf_or_ambiguous_definition_never_supplies_resources():
    kb=KB()
    result=review()
    result['sources'][0]['source_id']='other-pdf'
    with patch('normative_service.candidate',side_effect=formatted):
        hydrate(kb,result)
    assert len(result['sources'])==1
    result=review()
    kb.db.execute("insert into norms values ('duplicate','pdf','27-06-031-01')")
    with patch('normative_service.candidate',side_effect=formatted):
        hydrate(kb,result)
    assert len(result['sources'])==1


def numeric_formatted(base='га',amount='200',name='Семена газонных трав'):
    def serialize(kb,item):
        return dict(id=item['id'],code=item['code'],entity_kind='norm',designation='ГЭСН 27',
            unit_raw=base,extraction_status='native',checks=[],rules=[],resources=[
                dict(code='16.2.02.07',name=name,unit='кг' if base=='га' else 'т',
                    quantity_kind='numeric',quantity=amount,raw_quantity=amount,status='native',
                    citations=[dict(source_id='pdf',page=60)])])
    return serialize


def qualified_review():
    from test_areal_resource_consumption import fixture,definition
    work,result=fixture()
    result['sources'][0]['text']=definition('Посев луговых газонов тракторной сеялкой')
    scope='Устройство луговых газонов тракторной сеялкой. Площадь 1931 м2'
    result['sources'][1]['text']=scope
    result['parameter_proposals'][0]['scope_quote']=scope
    return work,result


def test_snapshot_recovery_rebinds_selected_definition_without_resource_alias_changes():
    from normative_evidence_snapshot import snapshot_answer,revalidate_answer
    from normative_resource_consumption import EVENT
    from test_areal_resource_consumption import run
    work,result=qualified_review()
    baseline=run(work,result)
    assert any(e.get('resource_rate_basis') for e in baseline['evidence_trace'])
    binding=dict(algorithm='old',inputs=dict(request=dict(question='check',pages=[])))
    answer=dict(summary='Qualified synthetic example',items=result['items'],
        parameters=result['parameter_proposals'],requests=[],adjustments=[])
    snapshot=snapshot_answer(answer,result['sources'],binding)
    reference=dict(key='snapshot',sha256=snapshot['sha256'])
    row=dict(id='old',payload=dict(position_check={**baseline,
        'coverage':dict(agent_evidence_snapshot=reference)}))
    original=deepcopy(row)
    context=SimpleNamespace(repository=SimpleNamespace(analysis_artifact=lambda *args:dict(
        identity=dict(stage='normative_agent_evidence'),payload=snapshot)))
    kb=KB('27-06-035-01')

    def current_validator(context,record,index,restored,actor,*,register):
        assert not register
        restored=restored['payload']['position_check']
        assert not any(e.get('kind')==EVENT for e in restored['evidence_trace'])
        selected=deepcopy(restored['items'])
        hydrate(kb,restored)
        once=deepcopy(restored)
        hydrate(kb,restored)
        assert restored==once and restored['items']==selected
        events=[e for e in restored['evidence_trace'] if e.get('kind')==EVENT]
        assert len(events)==1 and events[0]['norm_source_id']=='n'
        prefix=events[0]['recovery_prefix']
        resource_id=prefix+':resources:60'
        assert events[0]['resources'][0]['source_id']==resource_id
        assert len([s for s in restored['sources'] if s['id']==resource_id])==1
        assert not any(s['id']==prefix for s in restored['sources'])
        output=run(work,restored)
        assert next(c for c in output['parameter_cards'] if c['id']=='quantity')['state']=='confirmed'
        assert any(e.get('resource_rate_basis') for e in output['evidence_trace'])
        return output

    with patch('normative_service.candidate',side_effect=numeric_formatted()),\
            patch('normative_evidence_snapshot.normative_identity',return_value={**binding,'algorithm':'new'}),\
            patch('normative_position_check.validate_agent_saved',side_effect=current_validator):
        revalidate_answer(context,{'project_id':'synthetic'},work.index,row,'test')
    assert row==original


def test_recovery_cleanup_supports_old_prefix_and_keeps_original_conflicting_event():
    from normative_agent import resource_sources
    from normative_resource_consumption import EVENT
    kb=KB()
    result=review()
    trace=[]
    original_sources=resource_sources(formatted(kb,dict(id='norm',code=kb.code,checks=[],rules=[])),
        'n',evidence_trace=trace)
    trace[0]['resources'][0].update(quantity='999',raw_quantity='999',quantity_kind='numeric')
    original=deepcopy(trace[0])
    result['sources'].extend(original_sources)
    result['evidence_trace']=trace
    with patch('normative_service.candidate',side_effect=formatted):
        hydrate(kb,result)
        recovered=next(e for e in result['evidence_trace'] if e.get('recovery_prefix'))
        prefix=recovered.pop('recovery_prefix')
        recovered['norm_source_id']=prefix  # Historical recovery event shape.
        hydrate(kb,result)
        once=deepcopy(result)
        hydrate(kb,result)
    assert result==once
    events=[e for e in result['evidence_trace'] if e.get('kind')==EVENT]
    assert len(events)==2 and original in events
    assert next(e for e in events if e!=original)['norm_source_id']=='n'
    assert len({s['id'] for s in result['sources']})==len(result['sources'])


@pytest.mark.parametrize('mutation',['other_pdf','other_code','unselected','ambiguous_norm','ambiguous_selected'])
def test_recovered_areal_rate_never_opens_wrong_or_ambiguous_definition(mutation):
    from test_areal_resource_consumption import run,definition
    work,result=qualified_review()
    result['evidence_trace']=[]
    kb=KB('27-06-035-01')
    if mutation=='other_pdf':
        result['sources'][0]['source_id']='other-pdf'
    elif mutation=='other_code':
        source=result['sources'][0]
        source['label']=source['label'].replace('27-06-035-01','27-06-035-02')
        source['text']=source['text'].replace('27-06-035-01','27-06-035-02')
    elif mutation=='unselected':
        result['items'][0]['normative_source_ids']=[]
    elif mutation=='ambiguous_norm':
        kb.db.execute("insert into norms values ('other','pdf','27-06-035-01')")
    else:
        result['sources'].append({**result['sources'][0],'id':'other','text':definition()})
        result['items'][0]['normative_source_ids'].append('other')
    with patch('normative_service.candidate',side_effect=numeric_formatted()):
        hydrate(kb,result)
    output=run(work,result)
    assert next(c for c in output['parameter_cards'] if c['id']=='quantity')['state']!='confirmed'
    assert not any(e.get('resource_rate_basis') for e in output['evidence_trace'])


def test_recovered_rows_preserve_legacy_t_per_t_proof():
    from normative_resource_consumption import consumption_proof
    from test_normative_resource_consumption import fixture
    raw: Any=fixture()
    work,owner,result=raw
    result['sources'][0].update(label='ГЭСН 27 · 27-06-035-01',
        text='27-06-035-01 Измеритель: т')
    assert consumption_proof(work,owner,result,'1.03',work.formula,arithmetic_confirmed=True)
    result['evidence_trace']=[]
    kb=KB('27-06-035-01')
    with patch('normative_service.candidate',side_effect=numeric_formatted('т','1.03','Эмульсия битумно-дорожная')):
        hydrate(kb,result)
        hydrate(kb,result)
    selected=consumption_proof(work,owner,result,'1.03',work.formula,arithmetic_confirmed=True)
    assert selected and selected['card']['found_unit']=='т/т'
