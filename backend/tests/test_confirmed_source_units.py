from copy import deepcopy
from types import SimpleNamespace

import pytest

from analysis_replay import digest
from normative_confirmed_units import attach, binding, confirm, identity, unit_confirmed
from normative_parameter_feedback import literal_parameter_issues
from workspace_models import NormativeParameterProposal


class Repository:
    supports_analysis_replay=True
    def __init__(self):
        self.artifacts={}
    def save_analysis_artifact(self, project, ident, payload):
        return deepcopy(self.artifacts.setdefault(digest(ident),dict(identity=ident,payload=payload))['payload'])
    def analysis_artifact(self, project, key):
        return deepcopy(self.artifacts.get(key))


def fixture():
    source=dict(id='agent:p92:cell:verified',kind='project',version_id='pdf-v1',page=92,
        text='Газонный камень БР 100.20.8\nВсего:\n2163',origin='ocr',numeric_verified=True,
        unit='',unit_verified=False,rects=[[.1,.2,.3,.4]])
    return SimpleNamespace(repository=Repository()),dict(project_id='project',config=dict(pdf_versions={'pd':'pdf-v1'})),source


def test_confirmation_is_context_not_fabricated_literal_pdf_text():
    context,record,source=fixture()
    original=deepcopy(source)
    confirm(context,record,source,'м','user','Подтверждён контекст единицы в таблице')
    result=attach(context,record,dict(sources=[source],items=[dict(state='needs_facts')]))
    resolved=result['sources'][0]
    assert resolved['text']==source['text'] and resolved['rects']==source['rects']
    assert resolved['unit']=='' and resolved['unit_verified'] is False
    assert unit_confirmed(resolved,'м') and not unit_confirmed(resolved,'м2')
    assert result['items']==[dict(state='needs_facts')]
    assert source==original
    assert attach(context,record,result)==result
    proposal=NormativeParameterProposal(parameter='quantity',value='2163',unit='м',source_id=source['id'],
        quote=source['text'],scope_quote=source['text'],scope_state='verified')
    assert literal_parameter_issues(proposal,resolved)==[]
    assert literal_parameter_issues(proposal,source)


@pytest.mark.parametrize('field,value',[('version_id','pdf-v2'),('id','agent:p92:cell:other'),
    ('text','Другой столбец\n2163'),('page',93),('numeric_verified',False)])
def test_unit_confirmation_does_not_transfer_to_different_or_unverified_evidence(field,value):
    context,record,source=fixture()
    confirm(context,record,source,'м','user','Уточнение единицы')
    source[field]=value
    resolved=attach(context,record,dict(sources=[source]))['sources'][0]
    assert not unit_confirmed(resolved,'м')


def test_conflicting_fact_is_not_silently_overwritten():
    context,record,source=fixture()
    confirm(context,record,source,'м','user','Уточнение единицы')
    with pytest.raises(ValueError,match='different confirmation'):
        confirm(context,record,source,'шт','user','Иное уточнение')
    stored=context.repository.analysis_artifact(record['project_id'],digest(identity(record['project_id'],source)))
    assert stored['payload']['unit']=='м'
    assert stored['payload']['binding']==binding(source)


@pytest.mark.parametrize('change',['alias','changed_text','changed_rects','changed_version','wrong_physical_id'])
def test_confirmation_follows_only_unchanged_physical_envelope_through_logical_alias(change):
    context,record,source=fixture()
    confirm(context,record,source,'м','user','Уточнение единицы')
    alias=dict(deepcopy(source),id='shared:39:logical',physical_source_id=source['id'])
    if change=='changed_text':
        alias['text']+=' другая колонка'
    elif change=='changed_rects':
        alias['rects']=[[.4,.5,.6,.7]]
    elif change=='changed_version':
        alias['version_id']='pdf-v2'
    elif change=='wrong_physical_id':
        alias['physical_source_id']='agent:p92:cell:other'
    attached=attach(context,record,dict(sources=[alias]))['sources'][0]
    assert unit_confirmed(attached,'м')==(change=='alias')
    assert attached['text']==alias['text']


@pytest.mark.parametrize('declared,unit,expected',[('2163','м','confirmed'),
    ('2164','м','possible_mismatch'),('2163','м2','possible_mismatch')])
def test_quantity_is_rechecked_independently_after_unit_confirmation(declared,unit,expected):
    from audit_models import VorPosition
    from normative_parameters import build_cards
    context,record,source=fixture()
    position=VorPosition(index=0,number='1',work_name='Установка газонного камня БР 100.20.8',
        volume=declared,unit=unit,position_type='Работа')
    result=dict(pipeline_version=4,execution_state='complete',state='needs_facts',sources=[source],
        items=[dict(state='confirmed',claim_ids=['quantity','unit'],project_source_ids=[source['id']],
            normative_source_ids=[])],parameter_proposals=[dict(parameter='quantity',operation='direct',
            source_id=source['id'],value='2163',unit='м',quote=source['text'],scope_quote=source['text'],scope_state='verified')])
    before=next(c for c in build_cards(position,result,{0:position},{}) if c['id']=='quantity')
    assert before['state']=='needs_facts'
    confirm(context,record,source,'м','user','Контекст таблицы')
    resolved=attach(context,record,result)
    after=next(c for c in build_cards(position,resolved,{0:position},{}) if c['id']=='quantity')
    assert after['state']==expected
