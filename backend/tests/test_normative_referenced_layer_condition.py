"""A referenced technical clause closes its own condition, never a child rate."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from normative_layer_conditions import layer_condition_proof
from normative_local_checks import layer_group


def fixture():
    work='Устройство нижнего слоя основания из ЩПС h=13 см (двухслойного основания - слой1)'
    comment='Тех.часть ГЭСН 99 п. 1.99.18. При толщине нижнего слоя основания более 18 см его нормировать в два слоя'
    p=SimpleNamespace(index=0,work_name=work,comment=comment,position_type='Работа',formula='',volume='500',unit='м2')
    related=[dict(work=work.replace('слой1','слой2'),quantity='500',unit='м2',row=22)]
    base='99-01-001-01'
    adjustment='99-01-001-02'
    clause='1.99.18 При толщине нижнего слоя основания более 18 см его нормировать в два слоя по норме '+base+' с изменением толщины по норме '+adjustment+'.'
    base_text=base+' Устройство основания из ЩПС нижний слой толщиной 15 см\nИзмеритель: 1000 м2'
    step_text=adjustment+' На каждый 1 см изменения толщины слоя добавлять или исключать к норме '+base+'\nИзмеритель: 1000 м2'
    project='Нижний слой основания из ЩПС h=26 см'
    sources=[dict(id='p',kind='project',origin='native',text=project,version_id='pd-pin',page=1),
        dict(id='n',kind='normative',origin='native',source_id='norm-pdf',label='ГЭСН 99 · 1.99.18',text=clause),
        dict(id='base',kind='normative',origin='native',source_id='norm-pdf',label='ГЭСН 99 · '+base,text=base_text),
        dict(id='step',kind='normative',origin='native',source_id='norm-pdf',label='ГЭСН 99 · '+adjustment,text=step_text)]
    result=dict(pipeline_version=4,execution_state='complete',related_positions=related,sources=sources,
        parameter_cards=[dict(id='quantity',state='confirmed'),
            dict(id='thickness',state='confirmed',found_value='26',found_unit='см',source_ids=['p']),
            dict(id='unit_scale:base',state='confirmed')],
        parameter_proposals=[dict(parameter='thickness',operation='direct',scope_state='verified',value='26',unit='см',
            source_id='p',quote='h=26 см',scope_quote=project),
            dict(parameter='unit_scale',scope_state='verified',source_id='base',value='1000',unit='м2',quote='Измеритель: 1000 м2')],
        items=[dict(state='confirmed',claim_ids=['normative:applicability'],project_source_ids=['p'],normative_source_ids=['base','step'])],
        evidence_trace=[dict(kind='norm_thickness_adjustment_proposal_v1',base_source_id='base',adjustment_source_id='step',
            signed_steps=-2,base_quote='толщиной 15 см',adjustment_quote=step_text)])
    return p,result,layer_group(p,related)


def test_explicit_vor_clause_binds_validated_base_even_when_model_omits_its_citation():
    p,result,group=fixture()
    original=deepcopy(result)
    proof=layer_condition_proof(p,result,group)
    assert proof and proof['normative_source_ids']==['n']
    assert any(c.startswith('basis:comment:consumption:') for c in proof['claim_ids'])
    assert not any(c.startswith(('related:','derived:')) for c in proof['claim_ids'])
    assert result==original


@pytest.mark.parametrize('change',[
    'not_named','wrong_pdf','unreadable_clause','wrong_base','wrong_layer','bad_adjustment',
    'unknown_quantity','unknown_thickness','unknown_applicability','different_comment_threshold',
    'different_operator','unverified_measure','ambiguous_measure','single_layer_only','different_family'])
def test_no_positive_bridge_without_all_literal_scope_conditions(change):
    p,result,group=fixture()
    if change=='not_named':
        p.comment=p.comment.replace('1.99.18','1.99.19')
    elif change=='wrong_pdf':
        result['sources'][1]['source_id']='another-pdf'
    elif change=='unreadable_clause':
        result['sources'][1]['origin']='ocr_unreviewed'
    elif change=='wrong_base':
        result['sources'][1]['text']=result['sources'][1]['text'].replace('99-01-001-01','99-01-001-03')
    elif change=='wrong_layer':
        result['sources'][1]['text']=result['sources'][1]['text'].replace('нижнего','верхнего')
    elif change=='bad_adjustment':
        result['evidence_trace'][0]['signed_steps']=-1
    elif change=='unknown_quantity':
        result['parameter_cards'][0]['state']='needs_facts'
    elif change=='unknown_thickness':
        result['parameter_cards'][1]['state']='needs_facts'
    elif change=='unknown_applicability':
        result['items'][0]['state']='needs_facts'
    elif change=='different_comment_threshold':
        p.comment=p.comment.replace('18 см','20 см')
    elif change=='different_operator':
        p.comment=p.comment.replace('более','не менее')
    elif change=='unverified_measure':
        result['parameter_proposals'][1]['scope_state']='unresolved'
    elif change=='ambiguous_measure':
        extra=deepcopy(result['sources'][2])
        extra['id']='other-base'
        extra['label']=extra['label'].replace('001-01','001-03')
        extra['text']=extra['text'].replace('001-01','001-03')
        result['sources'].append(extra)
        result['parameter_proposals'].append({**result['parameter_proposals'][1],'source_id':'other-base'})
        result['parameter_cards'].append(dict(id='unit_scale:other-base',state='confirmed'))
        result['items'][0]['normative_source_ids'].append('other-base')
    elif change=='single_layer_only':
        result['sources'][1]['text']=result['sources'][1]['text'].replace('нижнего слоя','однослойного')
    elif change=='different_family':
        result['sources'][2]['label']=result['sources'][2]['label'].replace('ГЭСН','ФЕР')
    assert layer_condition_proof(p,result,group) is None


def powered_fixture():
    p,result,group=fixture()
    p.comment+='\nУстройство основания производится бульдозером мощностью 79 кВт'
    result['sources'].append(dict(id='base:resources:25',kind='normative',origin='extracted_native_unreviewed',
        source_id='norm-pdf',page=25,label='ГЭСН 99 · 99-01-001-01 · ресурсы',
        text='Ресурсы нормы 99-01-001-01; Измеритель: 1000 м2\n'
            '91.01.01-035 Бульдозеры, мощность 79 кВт (108 л.с.); маш.-ч: 1,21'))
    return p,result,group


def test_compound_predicate_requires_separate_literal_power_resource():
    p,result,group=powered_fixture()
    proof=layer_condition_proof(p,result,group)
    assert proof and proof['normative_source_ids']==['n','base:resources:25']
    assert '79 кВт' in proof['explanation']
    assert 'отдельно подтверждена ресурсом выбранной нормы' in proof['explanation']


def test_selected_adjustment_measure_does_not_masquerade_as_a_second_base():
    p,result,group=powered_fixture()
    result['parameter_cards'].append(dict(id='unit_scale:step',state='confirmed'))
    result['parameter_proposals'].append(dict(parameter='unit_scale',scope_state='verified',
        source_id='step',value='1000',unit='м2',quote='Измеритель: 1000 м2'))
    proof=layer_condition_proof(p,result,group)
    assert proof and proof['normative_source_ids']==['n','base:resources:25']


@pytest.mark.parametrize('change',['wrong_reference','wrong_family','wrong_pdf','negated_rule'])
def test_unverified_adjustment_relationship_remains_an_ambiguous_measure(change):
    p,result,group=powered_fixture()
    result['parameter_cards'].append(dict(id='unit_scale:step',state='confirmed'))
    result['parameter_proposals'].append(dict(parameter='unit_scale',scope_state='verified',
        source_id='step',value='1000',unit='м2',quote='Измеритель: 1000 м2'))
    step=result['sources'][3]
    if change=='wrong_reference':
        step['text']=step['text'].replace('к норме 99-01-001-01','к норме 99-01-001-03')
    elif change=='wrong_family':
        step['label']=step['label'].replace('ГЭСН','ФЕР')
    elif change=='wrong_pdf':
        step['source_id']='other-pdf'
    elif change=='negated_rule':
        step['text']=step['text'].replace('добавлять','не добавлять')
    assert layer_condition_proof(p,result,group) is None


@pytest.mark.parametrize('change',[
    'wrong_power','missing_resource','unknown_tail','negated_tail','other_machine','resource_negated',
    'wrong_resource_id','wrong_pdf','wrong_version','unreadable','wrong_label','ambiguous_label',
    'wrong_header','zero_rate','project_rate','range_rate','missing_page','different_base',
    'unverified_measure','unknown_applicability','different_family','page_id_mismatch','extra_id_suffix'])
def test_compound_tail_never_closes_from_its_two_layer_prefix(change):
    p,result,group=powered_fixture()
    resource=result['sources'][-1]
    if change=='wrong_power':
        p.comment=p.comment.replace('79 кВт','999 кВт')
    elif change=='missing_resource':
        result['sources'].pop()
    elif change=='unknown_tail':
        p.comment+=' и с дополнительным условием'
    elif change=='negated_tail':
        p.comment=p.comment.replace('производится','не производится')
    elif change=='other_machine':
        resource['text']=resource['text'].replace('Бульдозеры','Автогрейдеры')
    elif change=='resource_negated':
        resource['text']=resource['text'].replace('Бульдозеры','Не Бульдозеры')
    elif change=='wrong_resource_id':
        resource['id']='other:resources:25'
    elif change=='wrong_pdf':
        resource['source_id']='another-pdf'
    elif change=='wrong_version':
        resource['version_id']='another-edition'
    elif change=='unreadable':
        resource['origin']='ocr_unreviewed'
    elif change=='wrong_label':
        resource['label']=resource['label'].replace('001-01','001-02')
    elif change=='ambiguous_label':
        resource['label']+=' 99-01-001-02'
    elif change=='wrong_header':
        resource['text']=resource['text'].replace('001-01','001-02')
    elif change=='zero_rate':
        resource['text']=resource['text'].replace('1,21','0')
    elif change=='project_rate':
        resource['text']=resource['text'].replace('1,21','П')
    elif change=='range_rate':
        resource['text']=resource['text'].replace('1,21','1,21–2')
    elif change=='missing_page':
        resource.pop('page')
    elif change=='different_base':
        result['sources'][2]['source_id']='another-pdf'
    elif change=='unverified_measure':
        result['parameter_proposals'][1]['scope_state']='unresolved'
    elif change=='unknown_applicability':
        result['items'][0]['state']='needs_facts'
    elif change=='different_family':
        resource['label']=resource['label'].replace('ГЭСН','ФЕР')
    elif change=='page_id_mismatch':
        resource['page']=26
    elif change=='extra_id_suffix':
        resource['id']+=':unrelated'
    assert layer_condition_proof(p,result,group) is None
