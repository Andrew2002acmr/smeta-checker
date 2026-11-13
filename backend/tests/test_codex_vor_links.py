from codex_vor_links import quantity_links


def test_links_keep_the_right_operation_with_repeated_quantities():
    scope = [{'text': 'Тротуар с велодорожкой'}]
    rows = []
    for row, name, unit, value, formula, comment, kind in (
        (41, 'Устройство основания из ЩПС', 'м2', '52.42', '', '', 'Работа'),
        (44, 'Подгрунтовка основания', 'т', '0.041936', '52.42*0.8/1000', '', 'Работа'),
        (46, 'Устройство асфальтобетонного покрытия', 'м2', '52.42', '', '', 'Работа'),
        (47, 'Асфальтобетонная смесь', 'т', '6.3910464', '52.42/1000*121.92', '', 'Материал'),
        (48, 'Установка камня', 'м', '92', '', '', 'Работа'),
        (49, 'Камень БР100.20.8', 'м3', '0.45', '', 'Vед=0,016м3/шт.', 'Материал'),
    ):
        rows.append(dict(position_index=row, sheet='ВОР', row=row, section_headings=scope,
                         work_name=name, unit=unit, vor_value=value, formula=formula,
                         comment=comment, position_type=kind))

    links = quantity_links(rows)
    assert [item['row'] for item in links[44]] == [41]
    assert [item['row'] for item in links[47]] == [46]
    assert links[49][0]['row'] == 48
    nominal = links[49][0]['nominal_recalculation']
    assert nominal['expected_vor_value'] == '1.472'
    assert nominal['requires_project_length']


def test_links_separate_project_volume_from_area_and_respect_section_boundaries():
    def row(index, section, title, unit, value, kind, formula=''):
        return dict(position_index=index, sheet='ВОР', row=index, section_headings=[{'text': section}],
                    work_name=title, unit=unit, vor_value=value, position_type=kind, formula=formula)

    rows = [
        row(18, 'Проезд', 'Устройство основания', 'м2', '150', 'Работа'),
        row(19, 'Проезд', 'Щебеночно-песчаная смесь', 'м3', '25.2', 'Материал', '20*1.26'),
        row(20, 'Тротуар', 'Щебеночно-песчаная смесь', 'м3', '25.2', 'Материал', '20*1.26'),
        row(21, 'Тротуар', 'Укладка геосетки', 'м2', '150', 'Работа'),
        row(22, 'Тротуар', 'Геосетка', 'м2', '150', 'Материал'),
        row(23, 'Тротуар', 'Эмульсия', 'м2', '150', 'Материал'),
    ]
    links = quantity_links(rows)
    assert links[19][0]['row'] == 18 and links[19][0]['context'] == 'layer_material_volume'
    assert links[22][0]['row'] == 21 and links[22][0]['context'] == 'installed_material'
    assert set(links) == {19, 22}
    assert all(link['relation'] == 'candidate' for items in links.values() for link in items)
    assert all(link['engineering_status'] == 'unresolved' for items in links.values() for link in items)


def test_same_section_title_at_different_excel_addresses_does_not_share_candidates():
    items = [
        dict(position_index=1, sheet='ВОР', row=18, section_headings=[{'address': 'A17', 'text': 'Тротуар'}],
             work_name='Укладка геосетки', unit='м2', vor_value='150', position_type='Работа'),
        dict(position_index=2, sheet='ВОР', row=20, section_headings=[{'address': 'A19', 'text': 'Тротуар'}],
             work_name='Геосетка', unit='м2', vor_value='150', position_type='Материал', formula='150*1'),
    ]
    assert quantity_links(items) == {}
