from contextlib import contextmanager
from copy import deepcopy
import sqlite3
from unittest.mock import patch

from normative_resource_recovery import hydrate


class KB:
    identity={'sha256':'immutable','release_id':'test'}

    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.execute('create table norms (id text,source_id text,code text)')
        self.db.execute("insert into norms values ('norm','pdf','27-06-031-01')")
        self.reads=0

    @contextmanager
    def connect(self):
        yield self.db

    def entity(self,kind,norm_id):
        self.reads+=1
        return dict(id=norm_id,code='27-06-031-01',status='native',resources=[
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


def review():
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
