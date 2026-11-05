from normative_table_candidates import grouped_totals


def table():
    cells=[]
    for side,base in [('Слева',0),('Справа',2)]:
        cells.extend([
            dict(cell_id=f's{base}',role='header',row=0,column=base,colspan=2,text=side),
            dict(cell_id=f'm{base}',role='header',row=1,column=base,colspan=2,text='Материал Б h=0,18 м'),
            dict(cell_id=f'u{base}',role='header',row=2,column=base,colspan=1,text='Площадь м2'),
            dict(cell_id=f'u{base+1}',role='header',row=2,column=base+1,colspan=1,text='Объем м3')])
        for column in (base,base+1):
            cells.append(dict(cell_id=f'v{column}',role='cell',row=5,column=column,text=str(10+column),
                header_ids=[f's{base}',f'm{base}',f'u{column}'],numeric_verified=True))
    cells.append(dict(cell_id='total',role='cell',row=5,column=0,text='Всего:'))
    return dict(table_id='t',col_count=4,row_count=6,cells=cells)


def test_symmetric_totals_keep_material_thickness_and_measure_subcolumns():
    candidates=grouped_totals(table(),'Материал Б',unit='м2',height_cm='18')
    assert candidates[0]['columns']==[0,2]
    assert candidates[0]['calculated_sum']=='22'
    assert candidates[0]['values']==['10','12']
    assert candidates[0]['unit_verified']
    assert not candidates[0]['scope_verified']
    assert len(candidates)==1  # Known m3 columns cannot be offered as m2.


def test_intermediate_subtotals_are_not_added_as_grand_total_operands():
    value=table()
    value['cells'][-1]['text']='Итог:'
    assert grouped_totals(value,'Материал Б')==[]


def test_uncertain_cell_is_retained_as_candidate_without_becoming_proof():
    value=table()
    cell=next(c for c in value['cells'] if c['cell_id']=='v0')
    cell['numeric_verified']=False
    candidates=grouped_totals(value,'Материал Б',unit='м2')
    assert candidates[0]['values']==['10','12']
    assert candidates[0]['numeric_verified'] is False


def test_another_material_cannot_join_symmetric_group():
    value=table()
    next(c for c in value['cells'] if c['cell_id']=='m2')['text']='Материал В h=0,18 м'
    assert grouped_totals(value,'Материал Б')==[]


def test_known_construction_parameters_are_constraints_not_just_ranking_hints():
    assert grouped_totals(table(),'Материал Б',height_cm='24')==[]
    value=table()
    for c in value['cells']:
        if c['cell_id'] in {'m0','m2'}:
            c['text']='Смесь 0/63 h=0,18 м'
    assert grouped_totals(value,'Смесь 0/31,5',height_cm='18')==[]


def test_parenthesized_depth_and_material_family_constrain_retrieval_without_vor_amount():
    value=table()
    for c in value['cells']:
        if c['cell_id'] in {'m0','m2'}:
            c['text']='Верхний слой основания ЩПС (25см)'
    assert grouped_totals(value,'Устройство основания ЩПС',height_cm='25')
    assert grouped_totals(value,'Устройство основания ЩПС',height_cm='18')==[]
    for c in value['cells']:
        if c['cell_id'] in {'m0','m2'}:
            c['text']='Верхний слой покрытия А16Вл (6см)'
    assert grouped_totals(value,'Устройство основания ЩПС')==[]


def test_single_total_column_is_a_direct_candidate_not_a_synthetic_sum():
    value = table()
    value['cells'] = [c for c in value['cells'] if c['cell_id'] not in {'s2','m2','u2','u3','v2','v3'}]
    candidates = grouped_totals(value, 'Материал Б', unit='м2', include_single=True)
    assert len(candidates) == 1
    assert candidates[0]['columns'] == [0]
    assert candidates[0]['operation'] == 'direct'
    assert candidates[0]['calculated_sum'] == '10'
    assert candidates[0]['scope_verified'] is False
    value['cells'][-1]['text'] = 'Итог по участку:'
    assert grouped_totals(value, 'Материал Б', unit='м2', include_single=True) == []
