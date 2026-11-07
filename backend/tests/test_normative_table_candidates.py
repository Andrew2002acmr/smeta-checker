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


def test_broad_category_does_not_split_symmetric_material_groups():
    value=table()
    value['col_count']=6
    value['cells'].insert(0,dict(cell_id='category',role='header',row=-1,column=0,colspan=4,text='Категория работ'))
    for cell in value['cells']:
        if cell['cell_id'].startswith('v'):
            cell['header_ids'].insert(0,'category')
    candidates=grouped_totals(value,'Материал Б',unit='м2',height_cm='18')
    assert len(candidates)==1 and candidates[0]['columns']==[0,2]
    assert candidates[0]['calculated_sum']=='22'
    # Without unit proofs, the physical sub-column still prevents mixing
    # quantities from area and volume under the same category.
    for cell in value['cells']:
        if cell['cell_id'].startswith('u'):
            cell['text']='Измеритель неразборчив'
    groups=grouped_totals(value,'Материал Б')
    assert {tuple(c['columns'])for c in groups}=={(0,2),(1,3)}


def test_intermediate_subtotals_are_not_added_as_grand_total_operands():
    value=table()
    value['cells'][-1]['text']='Итог:'
    assert grouped_totals(value,'Материал Б')==[]


def test_pinned_ocr_reading_can_find_column_without_rewriting_or_proving_label():
    value=table()
    for cell in value['cells']:
        if cell['cell_id'] in {'m0','m2'}:
            cell['bbox']=[0,0,10,10]
            cell['header_line_observation']=dict(header_id=cell['cell_id'],bbox=cell['bbox'],
                image_sha256='crop',lines=[dict(rapid_words=[dict(text='А5Вл')])])
    found=grouped_totals(value,'А5Вл',unit='м2')
    assert found and found[0]['matched_header_observations'][0]['text']=='А5Вл'
    assert not found[0]['scope_verified']
    assert all('А5Вл' not in header for header in found[0]['headers'])
    for cell in value['cells']:
        if cell['cell_id'] in {'m0','m2'}:
            cell['header_line_observation']['bbox']=[1,1,2,2]
    assert not grouped_totals(value,'А5Вл',unit='м2')


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


def test_agent_discovery_can_see_a_different_physical_depth_without_proving_transformation():
    candidates=grouped_totals(table(),'Материал Б',unit='м2',height_cm='9',
        include_parameter_alternatives=True)
    assert len(candidates)==1 and candidates[0]['calculated_sum']=='22'
    assert candidates[0]['parameter_mismatches']==[dict(parameter='thickness',requested_cm='9',
        observed_cm=['18.00'],requires_normative_transform_check=True)]
    assert candidates[0]['discovery_only'] and not candidates[0]['scope_verified']
    # The strict proof-oriented caller still cannot accept another depth.
    assert not grouped_totals(table(),'Материал Б',unit='м2',height_cm='9')


def test_missing_depth_is_reported_as_unknown_instead_of_parameter_match():
    value=table()
    for cell in value['cells']:
        if cell['cell_id'] in {'m0','m2'}:
            cell['text']='Материал Б'
    candidates=grouped_totals(value,'Материал Б',unit='м2',height_cm='9',include_parameter_alternatives=True)
    assert candidates[0]['parameter_match_state']=='not_observed'
    assert candidates[0]['missing_parameters']==['thickness']
    assert candidates[0]['discovery_only'] and not candidates[0]['scope_verified']


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

def test_material_family_abbreviation_rescues_lexical_miss():
    # "ЩПС" stems to "щпс" while "щебеночно-песчаная" stems to "щебено":
    # without the shared family map the column is invisible to discovery.
    value = table()
    for cell in value['cells']:
        if cell['cell_id'] in {'m0', 'm2'}:
            cell['text'] = 'Щебеночно-песчаная смесь фр. 0/31,5'
    candidates = grouped_totals(value, 'Устройство основания из ЩПС', unit='м2', include_single=True)
    assert candidates
    assert candidates[0]['columns'] == [0, 2]
    assert candidates[0]['discovery_only'] is True
