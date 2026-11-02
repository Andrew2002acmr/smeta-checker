from copy import deepcopy
from types import SimpleNamespace

import pytest

from analysis_replay import digest
from normative_table_headers import apply, save
from pdf_table_verification import VERSION, SEMANTIC_VERSION


def setup():
    header=dict(cell_id='h',role='header',row=1,column=2,colspan=2,bbox=[0,0,10,5],text='',header_ids=[])
    proof=dict(version=VERSION,semantic_version=SEMANTIC_VERSION,header_id='h',row=1,column=2,
        colspan=2,bbox=header['bbox'],image_sha256='original-crop',
        variants=[dict(scale=s,words=[dict(text='h=0,07m',conf=95)]) for s in [1,1.35]])
    header['header_refinement']=deepcopy(proof)
    table=dict(table_id='t',cells=[header,dict(cell_id='v',role='cell',row=2,column=2,colspan=1,
        text='123',header_ids=[])])
    repaired=dict(**deepcopy(header))
    repaired.update(text='h=0,07m',raw_recovered_text='')
    artifacts={}
    def store(project,ident,payload):
        artifacts.setdefault(digest(ident),dict(identity=deepcopy(ident),payload=deepcopy(payload)))
        return artifacts[digest(ident)]['payload']
    context=SimpleNamespace(repository=SimpleNamespace(supports_analysis_replay=True,
        analysis_artifact=lambda project,key:artifacts.get(key),save_analysis_artifact=store))
    record=dict(project_id='project',config=dict(pdf_versions={'p.pdf':'pdf-version'}))
    return context,record,table,repaired,artifacts


def test_reuse_requires_exact_table_pdf_and_project_and_keeps_input_immutable():
    ctx,record,table,header,_=setup()
    frozen=deepcopy(table)
    save(ctx,record,'pdf-version',89,table,[header])
    events=[]
    result=apply(ctx,record,'pdf-version',89,[table],events)[0]
    assert result['cells'][0]['text']=='h=0,07m'
    assert result['cells'][1]['header_ids']==['h']
    assert table==frozen and len(events)==1
    apply(ctx,record,'pdf-version',89,[table],events)
    assert len(events)==1
    for wrong in [dict(table,table_id='other'),dict(table,cells=[*table['cells'],dict(cell_id='new')])]:
        assert apply(ctx,record,'pdf-version',89,[wrong],[])[0]==wrong
    assert apply(ctx,record,'other-version',89,[table],[])[0]==table
    assert apply(ctx,record,'pdf-version',90,[table],[])[0]==table
    assert apply(ctx,dict(record,project_id='other'),'pdf-version',89,[table],[])[0]==table


@pytest.mark.parametrize('change',[
    lambda h:h.update(column=3),lambda h:h.update(bbox=[0,0,11,5]),
    lambda h:h.update(role='cell'),lambda h:h.update(text='h=0,08m'),
    lambda h:h.update(raw_recovered_text='invented'),
    lambda h:h['header_refinement'].update(image_sha256='other-crop'),
    lambda h:h['header_refinement']['variants'][1]['words'][0].update(conf=1),
])
def test_reject_unproven_or_relocated_header(change):
    ctx,record,table,header,_=setup()
    change(header)
    with pytest.raises(ValueError):
        save(ctx,record,'pdf-version',89,table,[header])


def test_revalidate_saved_proof_instead_of_trusting_artifact_contents():
    ctx,record,table,header,artifacts=setup()
    save(ctx,record,'pdf-version',89,table,[header])
    next(iter(artifacts.values()))['payload']['headers'][0]['text']='h=0,08m'
    with pytest.raises(ValueError):
        apply(ctx,record,'pdf-version',89,[table],[])
