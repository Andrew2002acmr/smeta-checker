from copy import deepcopy

import cv2
import numpy as np
import pymupdf
import pytest

from pdf_ruled_recovery import (
    closed_geometries, closed_geometry, geometry_from_boxes, merge_recovered, raster_grid, rebuild, recover_page,
)


def sample_grid():
    boxes = [[col*100, row*30, (col+1)*100, (row+1)*30] for row in range(3) for col in range(3)]
    logical = [[row, row, col, col] for row in range(3) for col in range(3)]
    values = [['Пикет', 'Площадь, м2', 'Объем, м3'], ['ПК1', '50', '10'], ['Всего:', '2843,00', '142,15']]
    words = [dict(text=value, confidence=.99, bbox=[col*100+5, row*30+5, col*100+85, row*30+20])
        for row, line in enumerate(values) for col, value in enumerate(line)]
    return boxes, logical, words


def test_rebuild_separates_quantities_by_cells_without_confirming_ocr_or_mutating_source():
    boxes, logical, words = sample_grid()
    original = deepcopy(words)
    table = rebuild(boxes, logical, words, page=1)
    totals = [cell for cell in table['cells'] if cell['row'] == 2]
    assert [cell['text'] for cell in totals] == ['Всего:', '2843,00', '142,15']
    for col, unit in ((1, 'м2'), (2, 'м3')):
        headers = [c['text'] for c in table['cells'] if c['cell_id'] in totals[col]['header_ids']]
        assert len(headers) == 1 and unit in headers[0]
    assert all(cell['numeric_verified'] is False for cell in table['cells'])
    assert words == original


def test_word_crossing_rule_is_not_split_or_assigned_by_numeric_value():
    boxes, logical, _ = sample_grid()
    crossing = [dict(text='2843 142', confidence=.99, bbox=[80,35,140,50])]
    table = rebuild(boxes, logical, crossing, page=1)
    assert table['unassigned_words'] == crossing
    assert all(not c['text'] for c in table['cells'])


def test_merged_header_preserves_distinct_child_units_and_shared_parent():
    boxes = [[0,0,100,60],[100,0,300,30],[100,30,200,60],[200,30,300,60],
        [0,60,100,90],[100,60,200,90],[200,60,300,90]]
    geometry = geometry_from_boxes(boxes)
    assert geometry is not None
    values = ['Пикет','Основание','Площадь, м2','Объем, м3','Всего:','120','24']
    words = [dict(text=text,confidence=.99,bbox=[b[0]+8,b[1]+8,b[2]-8,b[3]-8])
        for b,text in zip(boxes,values,strict=True)]
    table = rebuild(*geometry,words,page=1)
    headers = {c['cell_id']:c for c in table['cells'] if c['role']=='header'}
    for number,unit in [('120','Площадь, м2'),('24','Объем, м3')]:
        cell = next(c for c in table['cells'] if c['text']==number)
        assert [headers[key]['text'] for key in cell['header_ids']] == ['Основание',unit]
        assert cell['numeric_verified'] is False


def test_geometry_identity_changes_when_cell_boundaries_change():
    boxes, logical, words = sample_grid()
    first = rebuild(boxes, logical, words, page=1)
    assert rebuild(boxes, logical, words, page=1)['table_id'] == first['table_id']
    boxes[4][2] -= 2
    second = rebuild(boxes, logical, words, page=1)
    assert second['table_id'] != first['table_id']
    assert {c['cell_id'] for c in first['cells']}.isdisjoint(c['cell_id'] for c in second['cells'])
    words[4]['text'] = '51'
    assert rebuild(boxes, logical, words, page=1)['table_id'] != second['table_id']


def test_ragged_exterior_corner_is_allowed_but_interior_hole_and_overlap_are_not():
    boxes, _, _ = sample_grid()
    assert geometry_from_boxes([box for i, box in enumerate(boxes) if i not in {1,2}]) is not None
    assert geometry_from_boxes([box for i, box in enumerate(boxes) if i != 4]) is None
    assert geometry_from_boxes(boxes + [boxes[4]]) is None


def test_blank_image_does_not_create_table_and_closed_rules_are_required():
    image = np.full((150,230),255,dtype=np.uint8)
    assert closed_geometry(raster_grid(image)) is None
    for x in (10,80,150,220):
        cv2.line(image,(x,10),(x,140),0,2)
    for y in (10,53,96,140):
        cv2.line(image,(10,y),(220,y),0,2)
    geometry = closed_geometry(raster_grid(image))
    assert geometry is not None and len(geometry[0]) == 9


def test_separate_grids_inside_one_scan_do_not_merge():
    image = np.full((340,230),255,dtype=np.uint8)
    for offset in (0,180):
        for x in (10,80,150,220):
            cv2.line(image,(x,10+offset),(x,140+offset),0,2)
        for y in (10,53,96,140):
            cv2.line(image,(10,y+offset),(220,y+offset),0,2)
    geometries = closed_geometries(raster_grid(image))
    assert len(geometries) == 2
    assert [len(boxes) for boxes, _ in geometries] == [9,9]
    assert max(box[3] for box in geometries[0][0]) < min(box[1] for box in geometries[1][0])


def test_partial_recovery_keeps_original_other_region_and_does_not_mutate_it():
    original = dict(table_id='old', origin='ocr', cells=[dict(numeric_verified=True,quality='usable',words=[
        dict(text='100',bbox=[0,0,10,10]),dict(text='200',bbox=[0,100,10,110])])])
    frozen = deepcopy(original)
    replacement = dict(table_id='new',cells=[])
    combined, retained = merge_recovered([original],[replacement],[[0,0,20,20]])
    assert retained == ['old'] and [t['table_id'] for t in combined] == ['new','old']
    assert combined[1]['cells'][0]['quality'] == 'uncertain'
    assert combined[1]['cells'][0]['numeric_verified'] is False
    assert 'unrecovered_source_region' in combined[1]['cells'][0]['reasons']
    assert original == frozen
    combined, retained = merge_recovered([original],[replacement],[[0,0,20,120]])
    assert retained == [] and combined == [replacement]


@pytest.mark.parametrize('page_rotation,ocr_rotation', [(0,0),(90,90),(270,270)])
def test_embedded_image_recovery_preserves_pdf_and_original_coordinate_frame(page_rotation,ocr_rotation):
    image = np.full((150,230),255,dtype=np.uint8)
    for x in (10,80,150,220):
        cv2.line(image,(x,10),(x,140),0,2)
    for y in (10,53,96,140):
        cv2.line(image,(10,y),(220,y),0,2)
    encoded = cv2.imencode('.png',image)[1].tobytes()
    with pymupdf.open() as document:
        page = document.new_page(width=300,height=220)
        page.insert_image(pymupdf.Rect(30,30,260,180),stream=encoded)
        page.set_rotation(page_rotation)
        words = [dict(text=value,confidence=.99,bbox=[30+x,30+y,30+x+25,30+y+12])
            for x,y,value in [(20,20,'Пикет'),(90,20,'Площадь'),(160,20,'Объем'),
                (20,65,'ПК1'),(90,65,'100'),(160,65,'20'),(20,108,'Всего:'),(90,108,'100'),(160,108,'20')]]
        original = [dict(table_id='old',origin='ocr',cells=[dict(words=words)],unassigned_words=[])]
        frozen = deepcopy(original)
        before = document.tobytes(no_new_id=True)
        result = recover_page(page,original,rotation=ocr_rotation)
        assert result['changed']
        assert document.tobytes(no_new_id=True) == before and page.rotation == page_rotation
        assert original == frozen
        recovered = [t for t in result['tables'] if t.get('recovery')]
        assert len(recovered) == 1
        assert all(0 <= c['bbox'][0] < c['bbox'][2] <= 300 and 0 <= c['bbox'][1] < c['bbox'][3] <= 220
            for c in recovered[0]['cells'])
        assert any(c['text'] == 'Всего:' for c in recovered[0]['cells'])
        assert all(not c['numeric_verified'] for c in recovered[0]['cells'])
        repeat = recover_page(page,result['tables'],rotation=ocr_rotation)
        assert repeat['tables'] == result['tables'] and repeat['changed'] is False
