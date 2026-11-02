"""A repaired grid creates new evidence without altering matching snapshots."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from audit_core import PageTextIndex
from normative_preparation import repair_table_index


@pytest.mark.parametrize('page_text',['Ведомость. Всего 12 24','Ведомость.\nВсего 12 24'])
def test_repaired_cells_replace_old_table_locators_but_keep_page_text_and_origin(page_text):
    old_table=dict(table_id='old',origin='ocr',row_count=1,col_count=1,
        cells=[dict(cell_id='old-cell',row=0,column=0,text='Всего 12 24',header_ids=[],role='cell')])
    source=PageTextIndex([page_text],table_rows=[['Всего 12 24']],
        tables=[[old_table]],locators=[[dict(table_id='old',cell_id='old-cell'),dict(table_id='',text='page text')]],
        quality={0:{'rotation':270}},metadata={'pdf_sha256':'a'*64,'preparation_complete':True})
    original=deepcopy(source.to_snapshot())
    new_table=dict(table_id='geometry-hash',origin='ocr',row_count=2,col_count=1,recovery={'version':'new'},
        cells=[dict(cell_id='header',row=0,column=0,text='Area m2',header_ids=[],role='header'),
               dict(cell_id='total',row=1,column=0,text='24',header_ids=['header'],role='cell',numeric_verified=False)])
    def verify(page,tables,**kwargs):
        assert tables[0]['cells'][1]['numeric_verified'] is False
        tables[0]['cells'][1]['numeric_verified']=True
    with patch('pdf_ruled_recovery.recover_page',return_value=dict(tables=[new_table],changed=True,diagnostics=[])) as recover,\
            patch('pdf_table_verification.verify_recovered_tables',side_effect=verify):
        updated,report=repair_table_index([object()],source)
    assert source.to_snapshot()==original
    assert updated.texts[0].startswith('Ведомость.') and updated.metadata['preparation_complete']
    assert updated.texts[0].endswith('Area m2\n24')
    if '\n' in page_text:
        assert 'Всего 12 24' not in updated.texts[0]
    else:
        assert updated.texts[0].startswith(page_text)
    assert updated.to_snapshot()['corpus_id']!=original['corpus_id']
    assert updated.metadata['table_recovery']['parent_corpus_id']==original['corpus_id']
    assert not any(c.get('cell_id')=='old-cell' for c in updated.locators[0])
    assert next(c for c in updated.locators[0] if c.get('cell_id')=='total')['numeric_verified']
    assert any(c.get('text')=='page text' for c in updated.locators[0])
    assert report['pages'][0]['verified_numbers']==1
    assert recover.call_args.kwargs['rotation']==270


def test_repair_keeps_unrecoverable_tables_and_does_not_search_for_vor_values():
    table=dict(origin='ocr',row_count=1,cells=[{'row':0,'text':'Всего 99'}])
    source=PageTextIndex(['Ведомость'],tables=[[table]],metadata={'pdf_sha256':'a'*64})
    original=deepcopy(source.to_snapshot())
    with patch('pdf_ruled_recovery.recover_page',return_value=dict(tables=[table],changed=False,
            diagnostics=['open grid'])) as recover,patch('pdf_table_verification.verify_recovered_tables') as verify:
        updated,report=repair_table_index([object()],source)
    assert source.to_snapshot()==original and updated.tables==source.tables
    assert updated.to_snapshot()==original
    assert not verify.called and report['pages'][0]['changed'] is False
    assert recover.call_args.args[1]==source.tables[0]


def test_existing_geometry_can_gain_new_numeric_proof_without_changing_original():
    table=dict(table_id='physical-grid',origin='ocr',row_count=2,col_count=1,recovery={'version':'existing'},
        cells=[dict(cell_id='h',row=0,column=0,text='Площадь, м2',role='header',header_ids=[]),
            dict(cell_id='n',row=1,column=0,text='24',role='cell',header_ids=['h'],numeric_verified=False)])
    source=PageTextIndex(['Ведомость\nПлощадь, м2\n24'],table_rows=[['Площадь, м2','24']],
        tables=[[table]],locators=[[]],metadata={'pdf_sha256':'a'*64})
    original=deepcopy(source.to_snapshot())
    def verify(page,tables,**kwargs):
        tables[0]['cells'][1]['numeric_verified']=True
    with patch('pdf_ruled_recovery.recover_page',return_value=dict(
            tables=[deepcopy(table)],changed=False,diagnostics=[])),\
            patch('pdf_table_verification.verify_recovered_tables',side_effect=verify):
        updated,report=repair_table_index([object()],source,refine_existing=True)
    assert source.to_snapshot()==original
    assert updated.tables[0][0]['cells'][1]['numeric_verified']
    assert updated.texts==source.texts
    assert report['pages'][0]['changed']
    assert updated.to_snapshot()['corpus_id']!=original['corpus_id']


@pytest.mark.parametrize('pages',[[0],[2],['1']])
def test_repair_rejects_invalid_physical_pages(pages):
    source=PageTextIndex(['Page'],metadata={'pdf_sha256':'a'*64})
    with pytest.raises(ValueError,match='Invalid physical page'):
        repair_table_index([object()],source,pages=pages)
