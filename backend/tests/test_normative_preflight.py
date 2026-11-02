"""Preflight preserves norm alternatives; literal adjustments are navigation only."""
from contextlib import contextmanager
from copy import deepcopy
import sqlite3
from unittest.mock import Mock

import pytest

from normative_parameter_anchors import thickness_adjustments
from normative_preflight import adjustment_references, preflight_candidates, same_table_adjustments, work_variants


def norm(suffix, *, source_id='pdf', table_code='77-01-046', text='', **extra):
    code=table_code+'-'+suffix
    return dict(id=source_id+':'+code,entity_kind='norm',code=code,source_id=source_id,
        family='ГЭСН',table_code=table_code,status='extracted_native_unreviewed',text=text,**extra)


def adjustment(**extra):
    return norm('05',text='Таблица ГЭСН 77-01-046\nИзмеритель: 100 м2\n77-01-046-05 '
        'На каждые 5 см изменения толщины слоя добавлять или исключать к нормам с 77-01-046-01 по 77-01-046-04\n'
        'Состав работ:\nПодвозка и насыпка.',**extra)


class Corpus:
    def __init__(self, norms=(), clauses=()):
        self.norms=list(norms)
        self.clauses=list(clauses)
        self.calls=[]

    def search(self, query, *, kind, facts, limit):
        self.calls.append((query,kind,deepcopy(facts),limit))
        return deepcopy((self.norms if kind=='GESN' else self.clauses)[:limit])

    @contextmanager
    def connect(self):
        with sqlite3.connect(':memory:') as db:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE norms(id TEXT,source_id TEXT,family TEXT,table_code TEXT,code TEXT,title TEXT)')
            for item in self.norms:
                db.execute('INSERT INTO norms VALUES(?,?,?,?,?,?)',(
                    item['id'], item['source_id'], item['family'], item['table_code'],
                    item['code'], item.get('title', item.get('text', ''))))
            yield db

    def entity(self,kind,entity_id):
        assert kind=='norm'
        return deepcopy(next(item for item in self.norms if item['id']==entity_id))

    def checks(self,item,facts):
        return [],['Engineering conditions unresolved']

    def rules(self,item,facts):
        return []


def test_work_query_keeps_second_norm_and_one_clause_without_selecting_applicability():
    kb=Corpus([norm('01'),norm('03'),norm('04')],[dict(id='clause1',entity_kind='clause')])
    result=work_variants(kb,'Подготовка почвы с растительной землей',{'unit':'м2'})
    assert [item['id'] for item in result]==[kb.norms[0]['id'],kb.norms[1]['id'],'clause1']
    assert [(kind,limit) for _,kind,_,limit in kb.calls]==[('GESN',2),('ALL',1)]
    assert all('scope_verified' not in item and 'automatic_application_allowed' not in item for item in result)


@pytest.mark.parametrize('query,kind',[('77-01-046-03','GESN'),('1.47.8','ALL'),('ГОСТ Р 12345-2020','ALL'),('СП 34.13330','ALL')])
def test_exact_and_document_queries_keep_original_scope_and_limit(query,kind):
    kb=Corpus([norm('01'),norm('03')],[dict(id='c',entity_kind='clause')])
    work_variants(kb,query,{})
    assert [(call[1],call[3]) for call in kb.calls]==[(kind,2)]


def test_unlabelled_comment_does_not_expand_to_additional_definitions():
    kb=Corpus([norm('01'),norm('03')],[dict(id='c',entity_kind='clause')])
    result=preflight_candidates(kb,['Комментарий'],{},definition_queries=['Работа'])
    assert [item['id'] for item in result]==[kb.norms[0]['id'],'c']


def test_catalog_budget_preserves_preceding_exact_references_and_deduplicates_variants():
    kb=Corpus([norm('01'),norm('03')],[dict(id='c',entity_kind='clause')])
    kb.search=Mock(side_effect=[
        [dict(id='exact1',entity_kind='clause'),dict(id='exact2',entity_kind='clause')],
        [norm('01'),norm('03')],[dict(id='c',entity_kind='clause')],
        [norm('01'),norm('03')],[dict(id='c',entity_kind='clause')]])
    result=preflight_candidates(kb,['1.47.8','Работа','Материал Работа'],{},
        definition_queries=['Работа','Материал Работа'],limit=4)
    assert [item['id'] for item in result]==['exact1','exact2',norm('01')['id'],norm('03')['id']]
    assert len(result)==4


def test_literal_adjustment_range_adds_navigation_candidate_with_unchanged_status():
    kb=Corpus([norm('01'),norm('03'),adjustment()],[dict(id='c',entity_kind='clause')])
    original=deepcopy(kb.norms)
    result=preflight_candidates(kb,['Работа'],{},definition_queries=['Работа'])
    assert [item['code'] for item in result if item['entity_kind']=='norm']==['77-01-046-01','77-01-046-03','77-01-046-05']
    found=result[-1]
    assert found['automatic_application_allowed'] is False and found['edition_confirmed'] is False
    assert found['missing_facts']==['Engineering conditions unresolved']
    assert found['status']=='extracted_native_unreviewed' and kb.norms==original


def test_cross_table_adjustment_uses_norm_title_from_corpus_schema():
    base = norm('03')
    step = norm('05', table_code='77-01-047', title=(
        'На каждые 5 см изменения толщины слоя добавлять или исключать '
        'к нормам с 77-01-046-01 по 77-01-046-04'
    ))
    step.pop('text')
    kb = Corpus([base, step])
    result = same_table_adjustments(kb, [base], {})
    assert [item['code'] for item in result] == ['77-01-047-05']
    assert result[0]['automatic_application_allowed'] is False


@pytest.mark.parametrize('change',[
    {'source_id':'other'}, {'family':'ФЕР'}, {'table_code':'77-01-047'},
    {'text':'На каждые 5 см изменения толщины слоя добавлять или исключать.'},
    {'text':'На каждые 5 см изменения толщины слоя добавлять к нормам 77-01-046-06'},
    {'text':'Норма 77-01-046-03 соседняя по номеру'},
])
def test_unreferenced_or_cross_document_adjustment_is_not_inferred(change):
    item=adjustment()
    item.update(change)
    assert not adjustment_references(item,norm('03'))


def test_resource_or_composition_reference_cannot_supply_definition_link():
    item=adjustment()
    item['text']='Состав работ:\n'+item['text']
    assert not adjustment_references(item,norm('03'))


def test_ambiguous_definition_and_oversized_table_are_not_automatically_expanded():
    step=adjustment()
    step['status']='native_definition_ambiguous'
    kb=Corpus([norm('03'),step])
    assert same_table_adjustments(kb,[norm('03')],{})==[]
    kb=Corpus([norm(str(index).zfill(2)) for index in range(1,34)]+[adjustment()])
    assert same_table_adjustments(kb,[norm('03')],{})==[]


def layer_sources():
    return [dict(id='base',source_id='pdf',kind='normative',origin='native',label='ГЭСН 77-01-046-03',
        text='Измеритель: 100 м2\n77-01-046-03 С внесением растительной земли слоем 15\nсм: механизированным способом'),
        dict(id='step',source_id='pdf',kind='normative',origin='native',label='ГЭСН 77-01-046-05',
        text='Измеритель: 100 м2\n77-01-046-05 На каждые 5 см изменения толщины слоя добавлять или исключать к нормам с 77-01-046-01 по 77-01-046-04')]


def test_literal_layer_word_preserves_quote_and_only_returns_arithmetic_candidate():
    sources=layer_sources()
    result=thickness_adjustments('Устройство газона h=0,20 м',sources)
    assert len(result)==1
    assert result[0]['base_quote']=='слоем 15\nсм'
    assert result[0]['base_quote'] in sources[0]['text']
    assert (result[0]['base_cm'],result[0]['step_cm'],result[0]['target_cm'],result[0]['signed_steps'])==('15.00','5.00','20.00','1')
    assert result[0]['scope_verified'] is False


@pytest.mark.parametrize('field,value',[('source_id','another-pdf'),('source_id',''),('label','77-01-047-05')])
def test_layer_arithmetic_requires_same_source_and_table(field,value):
    sources=layer_sources()
    sources[1][field]=value
    assert thickness_adjustments('Устройство газона h=0,20 м',sources)==[]


def test_noninteger_thickness_increment_is_not_filled_or_rounded():
    assert thickness_adjustments('Устройство газона h=0,22 м',layer_sources())==[]
