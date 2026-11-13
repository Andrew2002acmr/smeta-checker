from copy import deepcopy

import pytest

from pdf_paired_ocr import VERSION,literal,verified_reading,pending_cells
from pdf_row_measurements import observations


def raster_table():
    from test_pdf_row_measurements import table
    grid=table()
    grid['origin']='ocr'
    for cell in grid['cells']:
        cell.update(origin='ocr',bbox=[cell['column']*10,cell['row']*10,cell['column']*10+10,cell['row']*10+10])
    return grid


def certify(cell,kind):
    cell['paired_ocr']=dict(version=VERSION,kind=kind,cell_id=cell['cell_id'],bbox=cell['bbox'],
        literal_text=cell['text'],state='observed',image_sha256='crop-sha',image_key='crop-key',
        rapid_words=[dict(text=cell['text'],confidence=.99)],
        variants=[dict(height=h,psm=7,words=[dict(text=cell['text'],conf=95)])for h in (48,64)])


def test_only_both_independently_read_cells_verify_the_pair():
    grid=raster_table()
    unit,value=grid['cells'][-2:]
    assert len(pending_cells(grid))==2
    certify(value,'values')
    assert not observations(grid,[value])[0]['recognition_verified']
    certify(unit,'units')
    pair=observations(grid,[value])[0]
    assert pair['recognition_verified']and pair['recognition_state']=='cross_engine_paired_cells'
    assert not pending_cells(grid)and not pair['scope_verified']
    assert not value.get('numeric_verified')  # A pair is not a scalar amount.


@pytest.mark.parametrize('mutation',['low_rapid','high_conflict','duplicate_scale','different_cell',
    'different_box','changed_literal','missing_crop','original_conflict'])
def test_incomplete_or_conflicting_pair_readings_never_verify(mutation):
    grid=raster_table()
    cell=grid['cells'][-1]
    certify(cell,'values')
    proof=cell['paired_ocr']
    if mutation=='low_rapid':
        proof['rapid_words'][0]['confidence']=.8
    if mutation=='high_conflict':
        proof['variants'].append(dict(height=96,psm=7,words=[dict(text='115/23,53',conf=90)]))
    if mutation=='duplicate_scale':
        proof['variants'][1]['height']=48
    if mutation=='different_cell':
        proof['cell_id']='neighbor'
    if mutation=='different_box':
        proof['bbox']=[0,0,1,1]
    if mutation=='changed_literal':
        cell['text']='114/23,54'
    if mutation=='missing_crop':
        proof.pop('image_key')
    if mutation=='original_conflict':
        cell['words']=[dict(text='115/23,53',confidence=.99)]
    assert not verified_reading(cell,'values')


def test_failed_read_is_retained_without_an_endless_preparation_loop():
    grid=raster_table()
    for cell,kind in pending_cells(grid):
        certify(cell,kind)
        cell['paired_ocr']['state']='failed'
    assert not pending_cells(grid)
    assert not observations(grid,[grid['cells'][-1]])[0]['recognition_verified']
    changed=deepcopy(grid)
    changed['cells'][-1]['text']='115/23,53'
    assert len(pending_cells(changed))==1


def test_units_are_ordered_and_rates_do_not_become_pairs():
    assert literal('шт./т','units')==('шт','т')
    assert literal('т/шт.','units')==('т','шт')
    assert literal('M/T','units')==('м','т')
    assert literal('Т/M','units')==('т','м')
    assert literal('кг/м2','units')is None
    assert literal('114/23,53/','values')is None


@pytest.mark.parametrize('field,value',[('cell_id','neighbor'),('kind','units'),('image_key',None),('image_sha256',None)])
def test_invalid_finished_proof_returns_to_the_preparation_queue(field,value):
    grid=raster_table()
    for cell,kind in pending_cells(grid):
        certify(cell,kind)
    cell=grid['cells'][-1]
    cell['paired_ocr'][field]=value
    assert [c['cell_id']for c,_ in pending_cells(grid)]==[cell['cell_id']]


def test_different_modes_at_one_scale_are_not_two_independent_scales():
    cell=raster_table()['cells'][-1]
    certify(cell,'values')
    cell['paired_ocr']['variants'][1].update(height=48,psm=13)
    assert not verified_reading(cell,'values')


def test_padding_cannot_hide_a_high_confidence_conflicting_rapid_read():
    cell=raster_table()['cells'][-1]
    certify(cell,'values')
    cell['paired_ocr']['rapid_attempts']=[dict(words=[dict(text='115/23,53',confidence=.99)])]
    assert not verified_reading(cell,'values')


def test_time_slice_resumes_missing_reads_and_keeps_the_durable_crop(tmp_path,monkeypatch):
    import pymupdf
    import ocr_engine
    import pdf_cell_ocr
    import pdf_paired_ocr
    from PIL import Image
    from storage_blobs import FileBlobStore
    grid=raster_table()
    unit,value=grid['cells'][-2:]
    certify(value,'values')
    clock=[0]
    reads=[]
    rapid=[]
    stored=[]
    blobs=FileBlobStore(tmp_path/'blobs')
    def store(raw):
        stored.append(raw)
        return blobs.put(raw)
    def read(*args,**kwargs):
        clock[0]+=1
        reads.append(kwargs['psm'])
        return [dict(text='шт./т',conf=95)]
    def recognize(image):
        rapid.append(image.size)
        return [dict(text='шт./т',confidence=.99)]
    monkeypatch.setattr(pdf_paired_ocr.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(pdf_cell_ocr,'clean_glyphs',lambda image:Image.new('L',(80,25),255))
    monkeypatch.setattr(pdf_cell_ocr,'_rapid_numeric',recognize)
    monkeypatch.setattr(ocr_engine,'run_tsv',read)
    monkeypatch.setattr(ocr_engine,'parse_tsv',lambda words:words)
    monkeypatch.setattr('header_ocr_model.configured',lambda:None)
    with pymupdf.open() as doc:
        page=doc.new_page(width=100,height=100)
        first=pdf_paired_ocr.complete(page,[grid],store_image=store,seconds=2)
        assert first['remaining']and unit['paired_ocr']['state']=='in_progress'
        original=deepcopy(unit['paired_ocr'])
        # JSON roundtrip models persistence and a new preparation process.
        import json
        grid=json.loads(json.dumps(grid))
        second=pdf_paired_ocr.complete(page,[grid],store_image=store,seconds=20)
        assert second['remaining']=={}
        assert len(rapid)==3 and len(stored)==1 and len(reads)==6
        assert grid['cells'][-2]['paired_ocr']['variants'][:2]==original['variants']
        proof=grid['cells'][-2]['paired_ocr']
        import hashlib
        assert hashlib.sha256(blobs.get(proof['image_key'])).hexdigest()==proof['image_sha256']
        assert verified_reading(grid['cells'][-2],'units')
        before=deepcopy(grid)
        assert pdf_paired_ocr.complete(page,[grid],store_image=store)['attempted']==0
        assert before==grid and len(reads)==6


def test_supplementary_resume_keeps_old_conflicts_and_reuses_finished_passes(tmp_path,monkeypatch):
    import pymupdf
    import ocr_engine
    import pdf_cell_ocr
    import pdf_paired_ocr
    import pdf_paired_models
    from PIL import Image
    from storage_blobs import FileBlobStore
    grid=raster_table()
    unit,value=grid['cells'][-2:]
    unit['text']='ШT/T'
    certify(value,'values')
    model=dict(pdf_paired_models.MODELS[0],tessdata_dir='/pinned-test-model')
    plans=[dict(height=h,psm=7,preprocessing=mode,model=model)for h,mode in ((24,'gray'),(32,'binary'))]
    plans.append(dict(plans[-1],resampling='nearest'))
    clock=[0]
    reads=[]
    def read(*args,**kwargs):
        extra=kwargs.get('oem')is not None
        reads.append('extra'if extra else'base')
        if extra:
            clock[0]+=1
        return [dict(text='шт/т',conf=95 if extra else 82)]
    monkeypatch.setattr(pdf_paired_ocr.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(pdf_cell_ocr,'clean_glyphs',lambda image,**kwargs:Image.new('L',(80,25),255))
    monkeypatch.setattr(pdf_cell_ocr,'_rapid_numeric',lambda image:[dict(text='шт/т',confidence=.99)])
    monkeypatch.setattr(ocr_engine,'run_tsv',read)
    monkeypatch.setattr(ocr_engine,'parse_tsv',lambda words:words)
    monkeypatch.setattr('header_ocr_model.configured',lambda:None)
    monkeypatch.setattr(pdf_paired_models,'signature',lambda:'models-and-plans')
    monkeypatch.setattr(pdf_paired_models,'variants',lambda kind:iter(plans if kind=='units'else()))
    blobs=FileBlobStore(tmp_path/'blobs')
    with pymupdf.open()as doc:
        page=doc.new_page(width=100,height=100)
        pdf_paired_ocr.complete(page,[grid],store_image=blobs.put,seconds=1)
        assert unit['paired_ocr']['state']=='in_progress'and reads.count('extra')==1
        previous=deepcopy(unit['paired_ocr']['variants'])
        # An existing v4 checkpoint must retain its crop and every old pass.
        unit['paired_ocr']['version']='physical-paired-cell-ocr-v4'
        pdf_paired_ocr.complete(page,[grid],store_image=blobs.put,seconds=10)
        assert unit['text']=='шт/т'and unit['raw_text_before_paired_ocr']=='ШT/T'
        assert pdf_paired_ocr.verified_reading(unit,'units')
        assert reads.count('base')==6 and reads.count('extra')==3
        assert unit['paired_ocr']['variants'][:len(previous)]==previous
        assert [v.get('resampling')for v in unit['paired_ocr']['variants'][-2:]]==['lanczos','nearest']
        # Prior high-confidence conflicts remain authoritative after adding
        # stronger models; they are not replaced by the latest successful pass.
        unit['paired_ocr']['variants'].insert(0,dict(height=96,words=[dict(text='т/шт',conf=75)]))
        assert not pdf_paired_ocr.verified_reading(unit,'units')


@pytest.mark.parametrize('change',['none','partial_reread','different_literal','wrong_language',
    'unmarked_conflict','values','low_vote'])
def test_language_migration_retires_only_unparseable_bilingual_reads_after_full_reread(change):
    from pdf_paired_ocr import BASE_PLANS,UNIT_LANGUAGE_POLICY,consensus
    old=dict(height=64,psm=7,words=[dict(text='UIT/T',conf=75)])
    replacements=[dict(height=h,psm=p,language='rus',words=[dict(text='шт/т',conf=80)])for h,p in BASE_PLANS]
    votes=[dict(height=h,psm=7,model_sha256='existing-pinned-header',words=[dict(text='шт/т',conf=95)])for h in (24,32)]
    proof=dict(unit_language_policy=UNIT_LANGUAGE_POLICY,superseded_mixed_language_indices=[0],
        rapid_words=[dict(text='шт/т',confidence=.99)],variants=[old,*replacements,*votes])
    if change=='partial_reread':
        proof['variants'].pop(1)
    if change=='different_literal':
        old['words'][0]['text']='т/шт'
    if change=='wrong_language':
        replacements[0]['language']='rus+eng'
    if change=='unmarked_conflict':
        proof['superseded_mixed_language_indices']=[]
    if change=='low_vote':
        votes[1]['words'][0]['conf']=89
    kind='values'if change=='values'else'units'
    assert consensus(proof,kind)==(('шт','т')if change=='none'else None)


def test_units_use_russian_and_values_use_english_base_ocr(tmp_path,monkeypatch):
    import pymupdf
    import ocr_engine
    import pdf_cell_ocr
    import pdf_paired_ocr
    from PIL import Image
    from storage_blobs import FileBlobStore
    grid=raster_table()
    reads=[]
    current=['шт/т']
    def rapid(image):
        text=current[0]
        return [dict(text=text,confidence=.99)]
    def read(*args,**kwargs):
        language=kwargs['language']
        reads.append(language)
        return [dict(text='шт/т'if language=='rus'else'114/23,53',conf=95)]
    monkeypatch.setattr(pdf_cell_ocr,'clean_glyphs',lambda image,**kwargs:Image.new('L',(80,25),255))
    monkeypatch.setattr(pdf_cell_ocr,'_rapid_numeric',rapid)
    monkeypatch.setattr(ocr_engine,'run_tsv',read)
    monkeypatch.setattr(ocr_engine,'parse_tsv',lambda words:words)
    monkeypatch.setattr('header_ocr_model.configured',lambda:None)
    monkeypatch.setattr('pdf_paired_models.configured',lambda:())
    unit,value=grid['cells'][-2:]
    certify(value,'values')
    # Simulate a real v5 checkpoint with its old bilingual conflict.
    certify(unit,'units')
    unit['text']='шт/т'
    old=unit['paired_ocr']
    old.update(version='physical-paired-cell-ocr-v5',literal_text='шт/т',
        rapid_words=[dict(text='шт/т',confidence=.99)],
        variants=[dict(height=64,psm=7,words=[dict(text='UIT/T',conf=75)])])
    previous=deepcopy(old['variants'])
    blobs=FileBlobStore(tmp_path/'blobs')
    # Let complete establish the actual crop hash for this synthetic fixture.
    old.pop('image_key')
    old.pop('image_sha256')
    with pymupdf.open()as doc:
        page=doc.new_page(width=100,height=100)
        pdf_paired_ocr.complete(page,[grid],store_image=blobs.put,seconds=20)
        assert reads==['rus']*6
        assert unit['paired_ocr']['variants'][:len(previous)]==previous
        # New Russian reads establish multiple scales at the unchanged 90.
        assert pdf_paired_ocr.verified_reading(unit,'units')
        value['paired_ocr']={}
        current[0]='114/23,53'
        pdf_paired_ocr.complete(page,[grid],store_image=blobs.put,seconds=20)
        assert reads[6:]==['eng']*6
