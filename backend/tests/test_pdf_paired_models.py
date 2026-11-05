import hashlib
from copy import deepcopy

import pytest

import pdf_paired_models as models


def test_optional_models_are_absent_or_hash_verified(tmp_path,monkeypatch):
    payload=b'local pinned model'
    model=dict(models.MODELS[0],sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models,'MODELS',(model,))
    models._verified.cache_clear()
    assert models._verified(str(tmp_path))==()
    path=tmp_path/model['directory']/model['file']
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    models._verified.cache_clear()
    assert models._verified(str(tmp_path))[0]['sha256']==model['sha256']
    path.write_bytes(b'changed weights')
    models._verified.cache_clear()
    with pytest.raises(ValueError,match='hash mismatch'):
        models._verified(str(tmp_path))
    models._verified.cache_clear()


def test_model_and_optical_plan_change_identity_without_changing_raw_contract(monkeypatch):
    from analysis_replay import algorithm,prepared_identity
    record=dict(project_id='project')
    with monkeypatch.context()as patch:
        patch.setattr(models,'configured',lambda:())
        algorithm.cache_clear()
        before=prepared_identity(record,b'PDF','v')
        model=dict(models.MODELS[0],tessdata_dir='/pinned')
        patch.setattr(models,'configured',lambda:(model,))
        first=models.signature()
        algorithm.cache_clear()
        after=prepared_identity(record,b'PDF','v')
        assert before['algorithm']!=after['algorithm']and before['inputs']==after['inputs']
        patch.setattr(models,'variants',lambda kind:iter([dict(height=96,psm=7,preprocessing='gray',model=model)]))
        assert models.signature()!=first
    algorithm.cache_clear()


@pytest.mark.parametrize('mutation',['missing_crop','wrong_hash','wrong_engine','same_scale','conflicting_order',
    'low_tesseract','low_rapid','invalid_crop_hash','invalid_resampling'])
def test_supplementary_votes_keep_evidence_and_confidence_contract(mutation):
    from pdf_paired_ocr import consensus
    model=models.MODELS[0]
    proof=dict(rapid_words=[dict(text='шт/т',confidence=.99)],variants=[
        dict(height=height,psm=7,words=[dict(text='шт/т',conf=95)],model_id=model['id'],
            model_sha256=model['sha256'],oem=model['oem'],preprocessing='gray',
            transformed_image_key='crop',transformed_image_sha256='a'*64)for height in (24,32)])
    assert consensus(proof,'units')==('шт','т')
    original=deepcopy(proof)
    if mutation=='missing_crop':
        proof['variants'][1].pop('transformed_image_key')
    if mutation=='wrong_hash':
        proof['variants'][1]['model_sha256']='other'
    if mutation=='wrong_engine':
        proof['variants'][1]['oem']=0
    if mutation=='same_scale':
        proof['variants'][1]['height']=24
    if mutation=='conflicting_order':
        proof['variants'].append(dict(height=64,words=[dict(text='т/шт',conf=75)]))
    if mutation=='low_tesseract':
        proof['variants'][1]['words'][0]['conf']=89
    if mutation=='low_rapid':
        proof['rapid_words'][0]['confidence']=.89
    if mutation=='invalid_crop_hash':
        proof['variants'][1]['transformed_image_sha256']=None
    if mutation=='invalid_resampling':
        proof['variants'][1]['resampling']='unknown'
    assert consensus(proof,'units')is None
    assert consensus(original,'units')==('шт','т')


@pytest.mark.parametrize('conflict',['т/шт','UIT/T'])
def test_new_optics_cannot_outvote_any_prior_strong_conflict(conflict):
    from pdf_paired_ocr import consensus
    proof=dict(rapid_words=[dict(text='шт/т',confidence=.99)],variants=[
        dict(height=h,resampling='nearest',words=[dict(text='шт/т',conf=99)])for h in (24,32,64)])
    assert consensus(proof,'units')==('шт','т')
    proof['variants'].insert(0,dict(height=64,words=[dict(text=conflict,conf=75)]))
    assert consensus(proof,'units')is None


def test_resampling_changes_signature_but_never_counts_as_another_scale(monkeypatch):
    from pdf_paired_ocr import consensus
    model=dict(models.MODELS[0],tessdata_dir='/pinned')
    monkeypatch.setattr(models,'configured',lambda:(model,))
    plan=dict(height=32,psm=7,preprocessing='binary',model=model)
    monkeypatch.setattr(models,'variants',lambda kind:iter([plan]if kind=='units'else[]))
    before=models.signature()
    plan['resampling']='nearest'
    assert models.signature()!=before
    proof=dict(rapid_words=[dict(text='шт/т',confidence=.99)],variants=[
        dict(height=32,resampling=sampling,words=[dict(text='шт/т',conf=95)])for sampling in ('nearest','lanczos')])
    assert consensus(proof,'units')is None


def test_install_download_verifies_hash_and_preserves_existing_file(tmp_path,monkeypatch):
    import io
    from scripts import install_paired_ocr_models as installer
    payload=b'pinned weights'
    model=dict(models.MODELS[0],sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(installer,'MODELS',(model,))
    path=tmp_path/model['directory']/model['file']
    monkeypatch.setattr(installer,'urlopen',lambda *a,**k:io.BytesIO(b'bad payload'))
    with pytest.raises(ValueError,match='hash mismatch'):
        installer.install(tmp_path)
    assert not path.exists()
    calls=[]
    def fetch(*a,**k):
        calls.append(a[0])
        return io.BytesIO(payload)
    monkeypatch.setattr(installer,'urlopen',fetch)
    installer.install(tmp_path)
    installer.install(tmp_path)
    assert path.read_bytes()==payload and len(calls)==1


def test_gray_cleaning_preserves_source_intensities_and_removes_border_rules():
    import numpy as np
    from PIL import Image,ImageDraw
    from pdf_cell_ocr import clean_glyphs
    image=Image.new('L',(100,60),255)
    drawing=ImageDraw.Draw(image)
    drawing.rectangle((0,0,99,59),outline=0,width=2)
    drawing.rectangle((30,20,45,35),fill=128)
    binary=clean_glyphs(image)
    gray=clean_glyphs(image,preserve_gray=True)
    assert binary.size==gray.size==(16,16)
    assert np.array(gray).min()==128 and np.array(binary).max()==0
