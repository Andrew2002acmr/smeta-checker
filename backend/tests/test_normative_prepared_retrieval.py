from copy import deepcopy
from types import SimpleNamespace

from normative_prepared_retrieval import PreparedTableSearch, combine
from normative_retrieval import DocumentSearch


def snapshot():
    header=dict(cell_id='h',role='header',row=0,column=2,text='неразборчиво',bbox=[0,0,10,10],
        header_refinement=dict(header_id='h',bbox=[0,0,10,10],image_sha256='crop',
            variants=[dict(words=[dict(text='Щебеночно-песчаная смесь')])]))
    return SimpleNamespace(texts=('Щебеночно-песчаная смесь, общие указания','Объём, ведомость'),
        tables=([],[dict(table_id='late',cells=[header])]))


def test_crop_alternative_is_searchable_but_not_promoted_to_literal_fact():
    index=snapshot()
    before=deepcopy(index)
    found=combine(DocumentSearch(index.texts),PreparedTableSearch(index),'щебеночно-песчаная смесь',tables=True)
    hit=next(h for h in found['hits'] if h['page']==2)
    reading=hit['table_observations'][0]
    assert reading['table_id']=='late' and reading['cell_id']=='h'
    assert reading['observation_kind']=='ocr_alternative' and not reading['recognition_verified']
    assert index==before and 'Щебеночно' not in index.texts[1]
    assert found['absence_proven'] is False and found['scope_verified'] is False


def test_stale_crop_or_rejected_geometry_cannot_supply_search_alternative():
    index=snapshot()
    cell=index.tables[1][0]['cells'][0]
    cell['header_refinement']['bbox']=[0,0,11,10]
    assert not PreparedTableSearch(index).search('щебеночно-песчаная смесь')
    cell['geometry_validation']={'requires_source_review':True}
    assert not PreparedTableSearch(index).search('неразборчиво')


def test_fusion_paginates_after_combining_channels_without_duplicate_pages():
    index=snapshot()
    text,physical=DocumentSearch(index.texts),PreparedTableSearch(index)
    first=combine(text,physical,'щебеночно-песчаная смесь',tables=True,limit=1)
    second=combine(text,physical,'щебеночно-песчаная смесь',tables=True,offset=first['next_offset'],limit=1)
    assert first['hits'][0]['page']!=second['hits'][0]['page']


def test_duplicate_ocr_variants_cannot_hide_another_physical_table():
    index=snapshot()
    header=index.tables[1][0]['cells'][0]
    header['header_refinement']['variants']=[dict(words=[dict(text='Покрытие '+str(i))])for i in range(12)]
    index.tables[1].append(dict(table_id='other',cells=[dict(cell_id='other-h',role='header',row=0,column=0,
        text='Покрытие тротуара',bbox=[20,0,30,10])]))
    found=PreparedTableSearch(index).search('Покрытие')[0]
    assert found['observation_count']==2
    assert {o['table_id']for o in found['observations']}=={'late','other'}
    assert not found['observations_truncated']
    assert len(found['observations'][0]['readings'])<=3
