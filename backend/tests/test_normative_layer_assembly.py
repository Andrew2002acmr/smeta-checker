from copy import deepcopy

import pytest

from audit_models import VorPosition,PositionSource
from normative_layer_assembly import layer_assembly
from normative_parameters import build_cards


def fixture():
    source=PositionSource(document_sha256='a'*64,format='xlsx',sheet='S',row=10,header_row=1,
        position_id='first',sections=[dict(address='A2',text='Съезды')])
    first=VorPosition(index=0,position_type='Работа',unit='м2',volume='80',position_source=source,
        work_name='Устройство нижнего слоя основания из ЩПС h=11 см (двухслойного основания - слой1)')
    second=first.model_copy(deep=True)
    second.index=2
    second.volume='90'
    second.position_source.row=12
    second.position_source.position_id='second'
    second.work_name=first.work_name.replace('слой1','слой2')
    return first,second


def test_distinct_areas_can_establish_declared_thickness_but_never_shared_quantity():
    first,second=fixture()
    group=layer_assembly(first,{0:first,2:second})
    assert group['height']==22 and not group['quantity_shared']
    scope='Тип III — Конструкция съездов\nНижний слой основания из ЩПС h=22 см, площадь 90 м2'
    result=dict(pipeline_version=4,sources=[dict(id='p',kind='project',origin='native',text=scope)],items=[],
        parameter_proposals=[dict(parameter='thickness',value='22',unit='см',source_id='p',
            quote='h=22 см',scope_quote=scope,scope_state='verified'),
            dict(parameter='quantity',value='90',unit='м2',source_id='p',quote='площадь 90 м2',
                scope_quote=scope,scope_state='verified')])
    cards={c['id']:c for c in build_cards(first,result,{0:first,2:second},{})}
    assert cards['thickness']['state']=='confirmed'
    assert cards['quantity']['state']=='needs_facts'


@pytest.mark.parametrize('change',['section','document','unit','stage','role','intervening_work','unknown_row','duplicate_row'])
def test_unrelated_or_ambiguous_stages_are_not_joined(change):
    first,second=fixture()
    positions={0:first,2:second}
    if change=='section':
        second.position_source.sections=[]
    if change=='document':
        second.position_source.document_sha256='b'*64
    if change=='unit':
        second.unit='м3'
    if change=='stage':
        second.work_name=first.work_name
    if change=='role':
        second.work_name=second.work_name.replace('нижнего','верхнего')
    if change in {'intervening_work','unknown_row','duplicate_row'}:
        other=deepcopy(first)
        other.index=1
        other.work_name='Другая работа'
        other.position_source.row=11 if change!='duplicate_row' else 10
        if change=='unknown_row':
            other.position_source=None
        positions[1]=other
    assert layer_assembly(first,positions) is None


def test_other_project_thickness_does_not_confirm_the_assembly():
    first,second=fixture()
    text='Тип III — Конструкция съездов\nНижний слой основания из ЩПС h=24 см'
    result=dict(pipeline_version=4,sources=[dict(id='p',kind='project',origin='native',text=text)],items=[],
        parameter_proposals=[dict(parameter='thickness',value='24',unit='см',source_id='p',
            quote='h=24 см',scope_quote=text,scope_state='verified')])
    card=next(c for c in build_cards(first,result,{0:first,2:second},{}) if c['id']=='thickness')
    assert card['state']!='confirmed'


def test_unresolved_norm_threshold_does_not_revoke_literal_project_thickness():
    first,second=fixture()
    text='Тип III — Конструкция съездов\nНижний слой основания из ЩПС h=22 см'
    result=dict(pipeline_version=4,sources=[dict(id='p',kind='project',origin='native',text=text)],
        items=[dict(state='needs_facts',claim_ids=['geometry:comment:thickness:1'],project_source_ids=['p'])],
        parameter_proposals=[dict(parameter='thickness',value='22',unit='см',source_id='p',
            quote='h=22 см',scope_quote=text,scope_state='verified')])
    card=next(c for c in build_cards(first,result,{0:first,2:second},{}) if c['id']=='thickness')
    assert card['state']=='confirmed'
    result['items'][0]['claim_ids']=['geometry:thickness']
    card=next(c for c in build_cards(first,result,{0:first,2:second},{}) if c['id']=='thickness')
    assert card['state']=='needs_facts'


def test_same_thickness_from_other_section_cannot_establish_assembly_geometry():
    from normative_layer_assembly import assembly_scope_matches
    first,_=fixture()
    scope='Нижний слой основания из ЩПС h=22 см'
    source=dict(text='Тип IV — Конструкция остановок\n'+scope)
    assert not assembly_scope_matches(first,dict(scope_quote=scope),source)
    source['text']='Тип III — Конструкция съездов\n'+scope
    assert assembly_scope_matches(first,dict(scope_quote=scope),source)


def test_unique_literal_is_recovered_by_scope_not_by_expected_height():
    from normative_layer_assembly import recover_thickness
    first,second=fixture()
    text='Тип III — Конструкция съездов\nНижний слой основания из ЩПС h=30 см'
    result=dict(sources=[dict(id='p',kind='project',origin='native',version_id='pdf',page=5,text=text)])
    recover_thickness(first,result,{0:first,2:second})
    assert result['parameter_proposals'][0]['value']=='30'
    assert result['evidence_trace'][0]['expected_value_used_for_selection'] is False
    # A distinct section is never substituted, even with the expected number.
    result=dict(sources=[dict(id='p',kind='project',origin='native',version_id='pdf',page=5,
        text=text.replace('съездов','остановок').replace('30','22'))])
    recover_thickness(first,result,{0:first,2:second})
    assert not result.get('parameter_proposals')
