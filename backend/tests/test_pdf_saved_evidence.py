from copy import deepcopy

from pdf_cell_ocr import column_unit
from pdf_saved_evidence import replay_unit_proofs
from pdf_table_verification import VERSION


def fixture():
    variants = [dict(height=h, words=[
        dict(text='м',conf=90,left=23,top=43,width=27,height=22),
        dict(text='2',conf=96,left=57,top=23,width=19,height=20),
    ]) for h in (48,64)]
    proof = dict(version=VERSION,column=0,header_id='h',bbox=[0,0,100,25],
        image_sha256='physical-header',variants=[dict(words=[
            dict(text='м?',conf=80,left=20,top=20,width=40,height=40)])]*2,
        unit_crop=dict(region=[0,0,40,40],image_size=[40,40],variants=variants))
    header = dict(cell_id='h',role='header',row=0,column=0,colspan=1,
        text='Площадь, м?',bbox=[0,0,100,25],header_ids=[],header_refinement=proof)
    cell = dict(cell_id='c',role='cell',row=1,column=0,colspan=1,
        text='17',bbox=[0,25,100,50],header_ids=['h'],numeric_verified=False)
    return dict(table_id='t',recovery={'version':'test'},col_count=1,cells=[header,cell])


def test_replay_derives_own_column_proof_without_mutation_or_approving_number():
    table = fixture()
    before = deepcopy(table)
    tables, events = replay_unit_proofs([table])
    assert table == before
    derived = tables[0]
    assert column_unit(derived,derived['cells'][1]) == 'м2'
    assert derived['cells'][1]['numeric_verified'] is False
    assert events[0]['header_id'] == 'h' and events[0]['unit'] == 'м2'
    again, events = replay_unit_proofs(tables)
    assert again == tables and events == []


def test_replay_rejects_other_crop_column_and_conflicting_readings():
    for field,value in [('bbox',[10,0,110,25]),('column',1),('header_id','other')]:
        table = fixture()
        table['cells'][0]['header_refinement'][field] = value
        result,events = replay_unit_proofs([table])
        assert not events and not column_unit(result[0],result[0]['cells'][1])
    table = fixture()
    table['cells'][0]['header_refinement']['unit_crop']['variants'][1]['words'][1]['text']='3'
    result,events = replay_unit_proofs([table])
    assert not events and not column_unit(result[0],result[0]['cells'][1])


def test_replay_never_uses_raw_text_or_a_merged_material_header_as_unit():
    for merged in (False,True):
        table=fixture()
        if merged:
            table['cells'][0]['colspan']=2
        else:
            del table['cells'][0]['header_refinement']
            table['cells'][0]['text']='Площадь, м2'
        result,events=replay_unit_proofs([table])
        assert not events and not column_unit(result[0],result[0]['cells'][1])
