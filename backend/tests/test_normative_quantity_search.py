from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from normative_quantity_search import augment, table_query, relevant_table
from test_normative_local_checks import _table


def fixture(tmp_path, *, ambiguous=False):
    version = '00000000-0000-0000-0000-000000000001'
    work = 'Устройство нижнего слоя основания ЩПС h=12 см (двухслойного основания - слой1)'
    position = SimpleNamespace(index=0, work_name=work, unit='м2', volume='100.00',
        position_source=SimpleNamespace(sheet='S', sections=[]))
    record = dict(id='run', config=dict(pdf_versions={'test.pdf':version}, position_inputs=[dict(index=2,
        work_name=work.replace('слой1','слой2'),volume='100.00',unit='м2',position_source=dict(sheet='S',sections=[],row=3))]))
    source = dict(id='p1',kind='project',origin='native',version_id=version,page=1,
        text='Нижний слой основания из ЩПС фр. 0/31,5 h=24 см. '+ 'Конструкция дорожной одежды. '*8)
    result = dict(sources=[source],items=[])
    repo = SimpleNamespace(load_version=lambda _:SimpleNamespace(blob_key='pdf',filename='test.pdf'),
        cached_normative_page=lambda run,key,build:build())
    context = SimpleNamespace(repository=repo,blob_store=SimpleNamespace(get=lambda key:
        b'PDF' if key=='pdf' else json.dumps(_table()).encode()))
    doc = MagicMock()
    pages = [SimpleNamespace(get_text=lambda **kw:source['text']),
        SimpleNamespace(get_text=lambda **kw:'',get_pixmap=lambda **kw:SimpleNamespace(tobytes=lambda _:b'PNG'))]
    if ambiguous:
        pages.append(pages[-1])
    doc.__enter__.return_value = doc
    doc.__len__.side_effect=lambda:len(pages)
    doc.__getitem__.side_effect=lambda i:pages[i]
    return position,record,result,context,doc


def test_automatic_discovery_works_without_prepared_cache_or_user_selected_pages(tmp_path):
    p,r,result,c,doc=fixture(tmp_path)
    def extracted(*args, **kwargs):
        request=args[4]
        assert request.pages==[2]  # Discovered from headers, not an input page.
        return [dict(id='p2',kind='project',origin='ocr_unverified',version_id=result['sources'][0]['version_id'],page=2,words_key='words')]
    with patch('normative_quantity_search.pymupdf.open', return_value=doc), patch.dict('os.environ',{'NORMATIVE_PREPARED_CACHE_ROOT':str(tmp_path)}), \
        patch('ocr_engine.run_tsv',return_value='TSV'), patch('ocr_engine.parse_tsv',return_value=[dict(text='Ведомость Площадь Справа 0/315 Всего',conf=90)]), \
        patch('normative_position_check.project_sources',side_effect=extracted):
        output=augment(c,r,p,result)
        assert [v['value'] for v in output['quantity_composition']['terms']]==['60','40']
        assert output['quantity_composition']['total']=='100'
        assert output['quantity_composition']['state']=='confirmed'
        assert result==dict(sources=result['sources'],items=[])
        p.volume='110.00'
        r['config']['position_inputs'][0]['volume']='110.00'
        wrong=augment(c,r,p,result)
        assert wrong['quantity_composition']['total']=='100'
        assert wrong['quantity_composition']['state']=='possible_mismatch'


def test_two_suitable_tables_are_not_resolved_by_picking_the_expected_sum(tmp_path):
    p,r,result,c,doc=fixture(tmp_path,ambiguous=True)
    original=deepcopy(result)
    with patch('normative_quantity_search.pymupdf.open',return_value=doc), patch.dict('os.environ',{'NORMATIVE_PREPARED_CACHE_ROOT':str(tmp_path)}), \
        patch('ocr_engine.run_tsv',return_value='TSV'), patch('ocr_engine.parse_tsv',return_value=[dict(text='Площадь 0/31,5 Всего',conf=90)]), \
        patch('normative_position_check.project_sources',return_value=[dict(id=f'p{i}',kind='project',origin='ocr_unverified',page=i,words_key='words') for i in (2,3)]):
        assert augment(c,r,p,result)==original


def test_query_requires_project_parameters_and_matching_units(tmp_path):
    p,r,result,_,_=fixture(tmp_path)
    related=[dict(work=p.work_name.replace('слой1','слой2'),quantity=p.volume,unit=p.unit,row=3)]
    query=table_query(p,related,result['sources'])
    assert query and relevant_table('Ведомость площадь 0/315 справа',query)
    assert not relevant_table('Ведомость площадь 0/63 справа',query)
    p.position_source.sections=[{'text':'Устройство автобусных остановок'}]
    scoped=table_query(p,related,result['sources'])
    assert scoped and not relevant_table('Ведомость площадь 0/31,5 справа',scoped)
    assert relevant_table('Ведомость остановок площадь 0/31,5 справа',scoped)
    p.unit='т'
    assert table_query(p,related,result['sources']) is None
