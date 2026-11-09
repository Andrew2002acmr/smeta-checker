from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pdf_fact_acceptance import evaluate


def fixture():
    index=SimpleNamespace(texts=('PDF',),tables=([dict(table_id='t',origin='native',cells=[
        dict(cell_id='h',role='header',column=0,colspan=1,text='Площадь м2'),
        dict(cell_id='a',bbox=[0,0,10,10],column=0,header_ids=['h'],text='25',numeric_verified=True),
        dict(cell_id='b',bbox=[10,0,20,10],column=1,header_ids=[],text='25',numeric_verified=True)])],))
    annotations=dict(source_sha256='PDF',cases=[dict(id='material-a',query='Материал А',unit='м2',height_cm='18',
        expected=dict(page=1,cells=[dict(bbox=[0,0,10,10],value='25')]))])
    return index,annotations


def test_expected_numbers_do_not_enter_search_or_repair_the_snapshot():
    index,annotations=fixture()
    original=deepcopy(index)
    discover=Mock(return_value=[dict(page=1,table_id='t',cell_ids=['a'],unit_verified=True)])
    assert evaluate(index,'PDF',annotations,discover)['physical_values_verified']==1
    annotations['cases'][0]['expected']['cells'][0]['value']='99'
    result=evaluate(index,'PDF',annotations,discover)
    assert result['retrieved']==1 and result['physical_values_verified']==0 and index==original
    assert discover.call_args.kwargs==dict(unit='м2',height_cm='18',section_context='')
    assert discover.call_args.args==('Материал А',)


def test_equal_number_from_another_physical_material_is_not_a_success():
    index,annotations=fixture()
    def discover(*a,**k):
        return [dict(page=1,table_id='t',cell_ids=['b'],unit_verified=True)]
    result=evaluate(index,'PDF',annotations,discover)
    assert result['retrieved']==0 and result['physical_values_verified']==0
    with pytest.raises(ValueError,match='another PDF'):
        evaluate(index,'other PDF',annotations,discover)


def test_source_handle_roundtrip_rejects_neighbor_rect_even_with_same_number():
    from normative_source_addresses import cell_source_id
    from pdf_source_links import source_link
    index,annotations=fixture()
    index.metadata=dict(document_version_id='v',page_geometry={'0':dict(width=20,height=10,matrix=[1,0,0,1,0,0])})
    table=index.tables[0][0]
    cell=table['cells'][1]
    sid=cell_source_id('v',1,'t','a')
    link=source_link(index,'v',1,table,cell)
    source=dict(id=sid,version_id='v',page=1,literal_text='25',source_anchor=link,rects=link['rects'])
    discover=Mock(return_value=[dict(page=1,table_id='t',cell_ids=['a'],unit_verified=True,operand_source_ids=[sid])])
    assert evaluate(index,'PDF',annotations,discover,resolve_source=lambda _:source)['source_links_verified']==1
    source['rects']=[[.5,0,1,1]]
    result=evaluate(index,'PDF',annotations,discover,resolve_source=lambda _:source)
    assert result['physical_values_verified']==1 and result['source_links_verified']==0
    source['rects']=link['rects']
    source['version_id']='another-version'
    assert evaluate(index,'PDF',annotations,discover,resolve_source=lambda _:source)['source_links_verified']==0
