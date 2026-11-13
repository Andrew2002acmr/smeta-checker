from types import SimpleNamespace

import pytest

from pdf_source_links import source_link


@pytest.mark.parametrize('matrix,width,height,expected', [
    ([1,0,0,1,0,0],100,200,[.1,.1,.8,.2]),
    ([0,1,-1,0,200,0],200,100,[.8,.1,.9,.8]),
])
def test_anchor_binds_literal_cell_and_rotation(matrix,width,height,expected):
    index=SimpleNamespace(metadata={'page_geometry':{'0':dict(matrix=matrix,width=width,height=height)}})
    cell=dict(cell_id='c',text='17',bbox=[10,20,80,40],header_ids=['h'])
    result=source_link(index,'version',1,{'table_id':'table'},cell)
    assert result['state']=='located' and result['rects']==[expected]
    assert result['literal_text']=='17' and result['header_ids']==['h']
    assert result['version_id']=='version' and result['cell_id']=='c'


@pytest.mark.parametrize('box', [[-1,10,20,30],[10,10,120,30],[20,10,20,30],[],[0,0,float('nan'),2]])
def test_bad_geometry_is_not_clamped_to_a_different_area(box):
    index=SimpleNamespace(metadata={'page_geometry':{'0':dict(matrix=[1,0,0,1,0,0],width=100,height=200)}})
    result=source_link(index,'version',1,{'table_id':'table'},dict(cell_id='c',bbox=box,text='17'))
    assert result['rects']==[] and result['state']!='located'


def test_clipped_geometry_still_requires_source_review():
    index=SimpleNamespace(metadata={})
    result=source_link(index,'version',1,{'table_id':'table'},dict(cell_id='c',bbox=[0,0,5,5],text='17',
        geometry_validation={'requires_source_review':True}))
    assert result['state']=='source_geometry_requires_review' and not result['rects']
