"""Geometry rejection must neither crash neighbouring reads nor manufacture evidence."""
from copy import deepcopy

import pytest

from pdf_cell_ocr import column_axis,subcolumn_box
from pdf_tables import recover_unit_headers


@pytest.mark.parametrize('bbox',[[],[0,0,0,1],[0,0,float('nan'),1]])
def test_unlocatable_header_stays_unverified_while_neighbour_observation_is_retained(bbox):
    rejected=dict(cell_id='invalid',role='header',column=0,row=0,colspan=1,text='Unlocated header',
        bbox=bbox,geometry_validation={'requires_source_review':True})
    valid=dict(cell_id='valid',role='header',column=1,row=0,colspan=1,text='Area',bbox=[10,10,50,30])
    before=deepcopy(rejected)
    words=[dict(text='м²',conf=95,bbox=[15,15,25,25]),dict(text='м³',conf=99,bbox=[])]
    recover_unit_headers([dict(cells=[rejected,valid])],words)
    assert rejected['bbox']==before['bbox']or all(str(a)==str(b)for a,b in zip(rejected['bbox'],before['bbox'],strict=True))
    assert rejected.get('unit_observation')is None
    assert valid['unit_observation']['unit_candidate']=='м2'
    assert valid['unit_observation']['verified']is False
    assert words[1]['bbox']==[]  # Raw observations are retained, not repaired.


def test_rejected_body_cannot_move_column_crop_or_crash_column_axis():
    header=dict(cell_id='header',role='header',row=0,column=0,colspan=2,text='Unit',bbox=[0,0,100,20])
    good=dict(cell_id='good',role='cell',row=1,column=0,colspan=1,bbox=[0,20,50,40])
    neighbour=dict(cell_id='neighbour',role='cell',row=1,column=1,colspan=1,bbox=[50,20,100,40])
    rejected=dict(cell_id='bad',role='cell',row=2,column=0,colspan=1,bbox=[],numeric_verified=False,
        geometry_validation={'requires_source_review':True})
    table=dict(cells=[header,good,neighbour,rejected])
    before=deepcopy(table)
    assert column_axis(table)==0
    assert subcolumn_box(table,header,0,0)==[0,0,50,20]
    assert table==before
    assert subcolumn_box(dict(cells=[header,rejected]),header,0,0)is None
    clipped=dict(good,bbox=[60,20,90,40],geometry_validation={'requires_source_review':True})
    assert subcolumn_box(dict(cells=[header,clipped]),header,0,0)is None
    assert subcolumn_box(table,dict(header,bbox=[]),0,0)is None
