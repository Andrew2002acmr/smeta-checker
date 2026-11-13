from copy import deepcopy

import pytest

from pdf_row_measurements import observations


def table():
    return dict(table_id='t',origin='native',cells=[
        dict(cell_id='hu',role='header',row=0,column=2,text='Единица\nизмерения'),
        dict(cell_id='hv',role='header',row=0,column=3,text='Количество'),
        dict(cell_id='u',role='cell',row=1,column=2,text='шт./т',header_ids=['hu']),
        dict(cell_id='v',role='cell',row=1,column=3,text='114/23,53',header_ids=['hv'])])


def test_ordered_units_and_values_do_not_depend_on_requested_vor_quantity():
    source=table()
    original=deepcopy(source)
    pair=observations(source,[source['cells'][-1]])[0]
    assert pair['components']==[dict(index=0,value='114',unit='шт',dimension='count'),
        dict(index=1,value='23.53',unit='т',dimension='mass')]
    assert pair['recognition_verified'] and not pair['scope_verified'] and source==original
    source['cells'][-2]['text']='т/шт.'
    assert observations(source,[source['cells'][-1]])[0]['components'][0]['unit']=='т'
    source['cells'][-2]['text']='M/T'
    assert [c['unit']for c in observations(source,[source['cells'][-1]])[0]['components']]==['м','т']


@pytest.mark.parametrize('mutation',['different_row','rate','missing_value','duplicate_unit',
    'wrong_column_header','ambiguous_unit_cell','merged_value','bad_geometry'])
def test_ambiguous_or_unrelated_cells_never_make_a_pair(mutation):
    source=table()
    unit,value=source['cells'][-2:]
    if mutation=='different_row':
        unit['row']=2
    if mutation=='rate':
        unit['text']='кг/м2'
    if mutation=='missing_value':
        value['text']='114'
    if mutation=='duplicate_unit':
        unit['text']='шт/шт'
    if mutation=='wrong_column_header':
        source['cells'][0]['text']='Примечание'
    if mutation=='ambiguous_unit_cell':
        source['cells'].append(dict(unit,cell_id='u2',column=1))
    if mutation=='merged_value':
        value['rowspan']=2
    if mutation=='bad_geometry':
        unit['geometry_validation']=dict(requires_source_review=True)
    assert not observations(source,[value])


def test_scalar_ocr_flags_cannot_verify_a_compound_reading():
    source=table()
    source['origin']='ocr'
    source['cells'][-1]['numeric_verified']=True
    pair=observations(source,[source['cells'][-1]])[0]
    assert not pair['recognition_verified']
    assert pair['recognition_state']=='paired_ocr_requires_verification'


def test_frame_subcolumns_do_not_hide_one_physical_quantity_column():
    source=table()
    source['cells'][1]['colspan']=2
    source['cells'][-1]['colspan']=2
    assert observations(source,[source['cells'][-1]])[0]['components'][0]['value']=='114'
    source['cells'].append(dict(cell_id='sub',role='header',row=1,column=4,
        text='Другая величина',colspan=1))
    source['cells'][-2]['row']=2
    source['cells'][2]['row']=2
    assert not observations(source,[source['cells'][-2]])


def test_quantity_cannot_span_two_columns_under_a_narrow_header():
    source=table()
    source['cells'][-1]['colspan']=2
    assert not observations(source,[source['cells'][-1]])


def test_frame_row_subdivision_requires_one_complete_work_description():
    source=table()
    source['cells'][-2]['rowspan']=2
    source['cells'][-1]['rowspan']=2
    assert not observations(source,[source['cells'][-1]])
    source['cells'].extend([
        dict(cell_id='hd',role='header',row=0,column=1,text='Наименование работ'),
        dict(cell_id='d',role='cell',row=1,column=1,rowspan=2,text='Изделие',header_ids=['hd'])])
    assert observations(source,[source['cells'][3]])
    source['cells'][-1]['rowspan']=1
    source['cells'].append(dict(cell_id='d2',role='cell',row=2,column=1,text='Другая работа',header_ids=['hd']))
    assert not observations(source,[source['cells'][3]])
