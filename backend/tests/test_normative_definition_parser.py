from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pymupdf

from normative_definition_parser import definition_context,parse_rows,source_rows


def row(y,*parts,page=1):
    pieces:list[dict[str,Any]]=[dict(text=text,bbox=[x,y,right,y+9]) for x,right,text in parts]
    return dict(page=page,text=' '.join(p['text'] for p in pieces),pieces=pieces,
        bbox=[pieces[0]['bbox'][0],y,pieces[-1]['bbox'][2],y+9])


def sample():
    return [
        row(10,(10,120,'Таблица ГЭСН 77-01-001'),(180,350,'Установка элементов')),
        row(25,(10,95,'Состав работ:')),
        row(36,(40,350,'Для норм с 77-01-001-01 по 77-01-001-02:')),
        row(47,(40,300,'01. Укладка бетонного основания.')),
        row(58,(40,350,'Для нормы 77-01-001-03:')),
        row(69,(40,300,'01. Монтаж природного камня.')),
        row(80,(10,95,'Измеритель:'),(130,180,'100 м')),
        row(91,(130,350,'Установка бетонных элементов:')),
        row(102,(10,80,'77-01-001-01'),(150,350,'при жестком покрытии')),
        row(113,(10,80,'77-01-001-02'),(150,350,'при другом покрытии')),
        row(124,(130,350,'Установка природных элементов:')),
        row(135,(10,80,'77-01-001-03'),(150,350,'при другом покрытии')),
        row(146,(10,80,'77-01-001-04'),(140,350,'Добавлять на каждый слой')),
        row(157,(300,350,'77-01-'),(400,450,'77-01-')),
        row(165,(10,90,'Код ресурса'),(130,260,'Наименование'),(300,350,'001-01')),
    ]


def test_parent_scope_is_inherited_by_following_siblings_not_previous_one():
    definitions,gaps=parse_rows(sample())
    assert not gaps and len(definitions)==4
    first=definitions['77-01-001-01']
    second=definitions['77-01-001-02']
    third=definitions['77-01-001-03']
    fourth=definitions['77-01-001-04']
    assert first['proof']['parents']==second['proof']['parents']==['Установка бетонных элементов:']
    assert third['proof']['parents']==['Установка природных элементов:']
    assert fourth['proof']['parents']==[]
    assert 'природн' not in second['text'] and 'Добавлять' not in third['text']
    assert 'бетонного основания' in first['text'] and 'природного камня' not in first['text']
    assert 'природного камня' in third['text'] and 'бетонного основания' not in third['text']
    assert first['text'].startswith('Таблица ГЭСН 77-01-001 Установка элементов')
    assert 'Измеритель: 100 м' in first['text']
    assert first['proof']['rows'][0]['text'].startswith('77-01-001-01')


def test_multiline_heading_keeps_all_literal_parts_on_the_correct_subgroup():
    rows=sample()
    rows[10:11]=[row(124,(130,350,'Установка природных элементов высотой')),
        row(129,(130,190,'15 см:'))]
    definitions,gaps=parse_rows(rows)
    assert not gaps
    assert definitions['77-01-001-03']['proof']['parents']==['Установка природных элементов высотой','15 см:']
    assert '15 см' not in definitions['77-01-001-02']['text']


def test_ambiguous_colon_heading_is_never_appended_to_previous_norm():
    rows=sample()
    rows[10:11]=[row(124,(150,350,'Установка природных элементов высотой')),
        row(129,(158,190,'15 см:'))]
    definitions,gaps=parse_rows(rows)
    assert definitions=={} and gaps


def test_unbounded_cross_page_or_wrong_table_code_list_is_not_repaired():
    for mutate in [lambda r:r.pop(),lambda r:r[-1].update(page=3),
            lambda r:r.__setitem__(8,row(102,(10,80,'78-01-001-01'),(150,350,'при жестком покрытии')))]:
        rows=sample()
        mutate(rows)
        definitions,gaps=parse_rows(rows)
        assert not definitions and gaps


def test_repeated_code_is_rejected_permanently_not_restored_by_third_copy():
    rows=sample()
    copies=[]
    for page in (1,2,3):
        copy=deepcopy(rows)
        for r in copy:
            r['page']=page
        copies.extend(copy)
    definitions,gaps=parse_rows(copies)
    assert not definitions and gaps


def test_resource_header_numbers_and_trailing_unknown_heading_do_not_become_definition():
    rows=sample()
    rows.insert(-2,row(152,(150,350,'Новая неизвестная группа:')))
    definitions,gaps=parse_rows(rows)
    assert not definitions and gaps


def test_oversized_first_occurrence_prevents_later_duplicate_repair():
    rows=sample()
    rows[8]=row(102,(10,80,'77-01-001-01'),(150,550,'Описание '+('текста '*1000)))
    later=sample()
    for r in later:
        r['page']=2
    definitions,gaps=parse_rows(rows+later)
    assert not definitions and gaps


def test_bounded_native_list_can_continue_to_next_page_before_resource_header():
    rows=sample()
    rows[-2]['page']=2
    rows[-1]['page']=2
    definitions,gaps=parse_rows(rows)
    assert not gaps and len(definitions)==4


def test_ambiguous_context_retains_all_siblings_but_makes_no_parent_claim():
    rows=sample()
    context=definition_context(rows,'77-01-001-02')
    assert context and context['proof']['kind']=='context_only'
    assert all(code in context['text'] for code in ('77-01-001-01','77-01-001-02','77-01-001-03'))
    assert 'бетонных' in context['text'] and 'природных' in context['text']
    assert 'parents' not in context['proof']
    rows.pop()
    assert definition_context(rows,'77-01-001-02') is None


def test_tiny_hidden_native_text_does_not_supply_a_visible_heading():
    with pymupdf.open() as doc:
        page=doc.new_page(width=400,height=200)
        page.insert_text((20,30),'visible literal',fontsize=10)
        page.insert_text((20,50),'invisible misleading scope',fontsize=.5)
        rows=source_rows(doc)
        assert len(rows)==1 and rows[0]['text']=='visible literal'


def test_numeric_and_norm_reference_labels_keep_their_literal_parent_scope():
    rows=sample()
    rows[8]=row(102,(10,80,'77-01-001-01'),(150,350,'2,5-2,9 т/м3'))
    rows[9]=row(113,(10,80,'77-01-001-02'),(150,350,'1:3'))
    rows[11]=row(135,(10,80,'77-01-001-03'),(150,350,'77-01-001-01'))
    definitions,gaps=parse_rows(rows)
    assert not gaps and len(definitions)==4
    assert definitions['77-01-001-01']['proof']['own_label']=='2,5-2,9 т/м3'
    assert definitions['77-01-001-02']['proof']['parents']==['Установка бетонных элементов:']
    assert definitions['77-01-001-03']['proof']['parents']==['Установка природных элементов:']


def test_previous_continuation_is_split_from_following_outdented_heading():
    rows=sample()
    rows.insert(10,row(119,(150,350,'с обработкой поверхности')))
    definitions,gaps=parse_rows(rows)
    assert not gaps
    assert 'с обработкой поверхности' in definitions['77-01-001-02']['text']
    assert 'с обработкой поверхности' not in definitions['77-01-001-03']['text']
    assert 'природных элементов' not in definitions['77-01-001-02']['text']


def test_literal_references_are_allowed_in_heading_and_final_continuation():
    rows=sample()
    rows[10]=row(124,(130,350,'Добавлять к норме 77-01-001-01 при изменении толщины:'))
    rows.insert(-2,row(152,(140,350,'к норме 77-01-001-03')))
    definitions,gaps=parse_rows(rows)
    assert not gaps
    assert definitions['77-01-001-03']['proof']['parents']==[rows[10]['text']]
    assert definitions['77-01-001-04']['text'].split('Состав работ:')[0].rstrip().endswith('к норме 77-01-001-03')


def test_missing_label_is_not_swallowed_as_a_continuation():
    rows=sample()
    rows.insert(-2,row(152,(10,80,'77-01-001-05')))
    definitions,gaps=parse_rows(rows)
    assert not definitions and gaps


def test_resource_code_column_cannot_supply_a_numeric_definition_anchor():
    rows=sample()
    rows.insert(-2,row(152,(300,370,'77-01-001-05'),(400,450,'1,5')))
    definitions,gaps=parse_rows(rows)
    assert not definitions and gaps


def test_final_wrapped_norm_reference_in_label_column_is_not_resource_header():
    rows=sample()
    rows.insert(-2,row(152,(140,210,'77-01-001-03')))
    definitions,gaps=parse_rows(rows)
    assert not gaps
    assert definitions['77-01-001-04']['proof']['own_label'].endswith('77-01-001-03')
    assert any(r['text']=='77-01-001-03' for r in definitions['77-01-001-04']['proof']['rows'])


def test_wrapped_measure_requires_literal_balanced_parentheses_same_column():
    rows=sample()
    rows[6]=row(80,(10,95,'Измеритель:'),(130,350,'100 м (нормы с 77-01-001-01 по'))
    rows.insert(7,row(85,(130,350,'77-01-001-04)')))
    definitions,gaps=parse_rows(rows)
    assert not gaps and len(definitions)==4
    assert '77-01-001-04)' in definitions['77-01-001-01']['text']
    rows[7]=row(85,(150,350,'77-01-001-04)'))
    definitions,gaps=parse_rows(rows)
    assert not definitions and gaps


def real_geometry(edition,table):
    fixture=Path(__file__).with_name('fixtures')/'native-definition-real-geometry-378.json'
    data=json.loads(fixture.read_text(encoding='utf-8'))
    return next(case['rows'] for case in data['cases']
        if case['edition']==edition and case['table']==table)


def test_equal_indent_real_continuation_cannot_become_next_siblings_parent():
    for table,code,lost in [
        ('27-06-039','27-06-039-01','комплексом с толщиной верхнего слоя'),
        ('27-03-002','27-03-002-01','битума, обрабатываемых смешением'),
    ]:
        rows=real_geometry('official378',table)
        assert any(lost in r['text'] for r in rows)
        definitions,gaps=parse_rows(rows)
        assert code not in definitions
        assert any(g.get('detail')=='previous_label_and_following_heading_same_indent'
            and code in g['codes'] for g in gaps)


def test_real_outdented_heading_keeps_previous_description_and_next_parent():
    for table,code,literal in [
        ('27-06-039','27-06-039-01','асфальтобетонной смеси толщиной 7 см'),
        ('27-03-002','27-03-002-01','обрабатываемых смешением автогрейдерами на месте из новых материалов'),
    ]:
        definitions,gaps=parse_rows(real_geometry('original1046',table))
        assert not gaps
        assert literal in definitions[code]['proof']['own_label']
        assert not definitions[code]['proof']['parents']
        assert definitions[table+'-02']['proof']['parents']
        assert literal not in ' '.join(definitions[table+'-02']['proof']['parents'])


def test_real_page_footer_between_table_title_and_next_page_is_not_title():
    rows=real_geometry('official378','27-09-012')
    footer=next(r for r in rows if r['text']=='2387')
    assert footer['bbox'][1]>.9*footer['page_size'][1]
    definitions,gaps=parse_rows(rows)
    assert not gaps
    definition=definitions['27-09-012-01']
    assert '2387' not in definition['text']
    assert all(r['text']!='2387' for r in definition['proof']['rows'])
    assert definition['title'].startswith('Установка дополнительных щитков')
    assert 'Измеритель: 100 шт' in definition['text']
    assert '01. Крепление щитков.' in definition['text']
