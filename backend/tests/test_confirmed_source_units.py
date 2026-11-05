from copy import deepcopy
from types import SimpleNamespace

import pytest

from analysis_replay import digest
from normative_confirmed_units import attach, binding, confirm, identity, unit_confirmed
from normative_parameter_feedback import literal_parameter_issues
from workspace_models import NormativeParameterProposal


class Repository:
    supports_analysis_replay=True
    def __init__(self):
        self.artifacts={}
    def save_analysis_artifact(self, project, ident, payload):
        return deepcopy(self.artifacts.setdefault(digest(ident),dict(identity=ident,payload=payload))['payload'])
    def analysis_artifact(self, project, key):
        return deepcopy(self.artifacts.get(key))


def fixture():
    source=dict(id='agent:p92:cell:verified',kind='project',version_id='pdf-v1',page=92,
        text='Газонный камень БР 100.20.8\nВсего:\n2163',origin='ocr',numeric_verified=True,
        unit='',unit_verified=False,rects=[[.1,.2,.3,.4]])
    return SimpleNamespace(repository=Repository()),dict(project_id='project',config=dict(pdf_versions={'pd':'pdf-v1'})),source


def test_confirmation_is_context_not_fabricated_literal_pdf_text():
    context,record,source=fixture()
    original=deepcopy(source)
    confirm(context,record,source,'м','user','Подтверждён контекст единицы в таблице')
    result=attach(context,record,dict(sources=[source],items=[dict(state='needs_facts')]))
    resolved=result['sources'][0]
    assert resolved['text']==source['text'] and resolved['rects']==source['rects']
    assert resolved['unit']=='' and resolved['unit_verified'] is False
    assert unit_confirmed(resolved,'м') and not unit_confirmed(resolved,'м2')
    assert result['items']==[dict(state='needs_facts')]
    assert source==original
    assert attach(context,record,result)==result
    proposal=NormativeParameterProposal(parameter='quantity',value='2163',unit='м',source_id=source['id'],
        quote=source['text'],scope_quote=source['text'],scope_state='verified')
    assert literal_parameter_issues(proposal,resolved)==[]
    assert literal_parameter_issues(proposal,source)


@pytest.mark.parametrize('field,value',[('version_id','pdf-v2'),('id','agent:p92:cell:other'),
    ('text','Другой столбец\n2163'),('page',93),('numeric_verified',False)])
def test_unit_confirmation_does_not_transfer_to_different_or_unverified_evidence(field,value):
    context,record,source=fixture()
    confirm(context,record,source,'м','user','Уточнение единицы')
    source[field]=value
    resolved=attach(context,record,dict(sources=[source]))['sources'][0]
    assert not unit_confirmed(resolved,'м')


def test_conflicting_fact_is_not_silently_overwritten():
    context,record,source=fixture()
    confirm(context,record,source,'м','user','Уточнение единицы')
    with pytest.raises(ValueError,match='different confirmation'):
        confirm(context,record,source,'шт','user','Иное уточнение')
    stored=context.repository.analysis_artifact(record['project_id'],digest(identity(record['project_id'],source)))
    assert stored['payload']['unit']=='м'
    assert stored['payload']['binding']==binding(source)


@pytest.mark.parametrize('change',['alias','changed_text','changed_rects','changed_version','wrong_physical_id'])
def test_confirmation_follows_only_unchanged_physical_envelope_through_logical_alias(change):
    context,record,source=fixture()
    confirm(context,record,source,'м','user','Уточнение единицы')
    from normative_source_identity import shared_source
    alias=shared_source(source,39)
    if change=='changed_text':
        alias['text']+=' другая колонка'
    elif change=='changed_rects':
        alias['rects']=[[.4,.5,.6,.7]]
    elif change=='changed_version':
        alias['version_id']='pdf-v2'
    elif change=='wrong_physical_id':
        alias['physical_source_id']='agent:p92:cell:other'
    attached=attach(context,record,dict(sources=[alias]))['sources'][0]
    assert unit_confirmed(attached,'м')==(change=='alias')
    assert attached['text']==alias['text']


@pytest.mark.parametrize('declared,unit,expected',[('2163','м','confirmed'),
    ('2164','м','possible_mismatch'),('2163','м2','possible_mismatch')])
def test_quantity_is_rechecked_independently_after_unit_confirmation(declared,unit,expected):
    from audit_models import VorPosition
    from normative_parameters import build_cards
    context,record,source=fixture()
    position=VorPosition(index=0,number='1',work_name='Установка газонного камня БР 100.20.8',
        volume=declared,unit=unit,position_type='Работа')
    result=dict(pipeline_version=4,execution_state='complete',state='needs_facts',sources=[source],
        items=[dict(state='confirmed',claim_ids=['quantity','unit'],project_source_ids=[source['id']],
            normative_source_ids=[])],parameter_proposals=[dict(parameter='quantity',operation='direct',
            source_id=source['id'],value='2163',unit='м',quote=source['text'],scope_quote=source['text'],scope_state='verified')])
    before=next(c for c in build_cards(position,result,{0:position},{}) if c['id']=='quantity')
    assert before['state']=='needs_facts'
    confirm(context,record,source,'м','user','Контекст таблицы')
    resolved=attach(context,record,result)
    after=next(c for c in build_cards(position,resolved,{0:position},{}) if c['id']=='quantity')
    assert after['state']==expected


@pytest.mark.parametrize("change", ["unattested_alias", "wrong_canonical", "changed_alias_content"])
def test_forged_alias_cannot_reuse_a_human_unit_confirmation(change):
    from normative_source_identity import shared_source
    context, record, source = fixture()
    confirm(context, record, source, "м", "user", "Контекст таблицы")
    resolved = attach(context, record, dict(sources=[source]))["sources"][0]
    if change == "unattested_alias":
        alias = dict(deepcopy(resolved), id="shared:39:logical", physical_source_id=source["id"])
    else:
        alias = shared_source(resolved, 39)
        if change == "wrong_canonical":
            alias["physical_source_id"] = "agent:p92:cell:another"
        else:
            alias["rects"] = [[.5, .6, .7, .8]]
    assert binding(alias) is None
    assert not unit_confirmed(alias, "м")
    assert not unit_confirmed(attach(context, record, dict(sources=[alias]))["sources"][0], "м")
    with pytest.raises(ValueError, match="canonical"):
        identity(record["project_id"], alias)
    with pytest.raises(ValueError, match="requires"):
        confirm(context, record, alias, "м", "user", "Не доверять alias")


def test_direct_historical_confirmation_identity_remains_unchanged():
    context, record, source = fixture()
    legacy_binding = {key: deepcopy(source.get(key)) for key in
        ("id", "version_id", "page", "text", "rects", "origin", "numeric_verified", "unit", "unit_verified")}
    legacy_identity = dict(schema=1, stage="confirmed_source_unit", project_id=record["project_id"],
        version_id=source["version_id"], source_sha256=digest(legacy_binding))
    assert binding(source) == legacy_binding
    assert identity(record["project_id"], source) == legacy_identity


def test_late_context_and_preexisting_context_both_survive_attested_aliases():
    from normative_source_identity import canonical_source_id, shared_source
    context, record, source = fixture()
    confirm(context, record, source, "м", "user", "Контекст таблицы")
    alias_before = shared_source(source, 39)
    late = attach(context, record, dict(sources=[alias_before]))["sources"][0]
    assert canonical_source_id(late) == source["id"]
    assert unit_confirmed(late, "м")
    assert attach(context, record, dict(sources=[late]))["sources"][0] == late
    direct = attach(context, record, dict(sources=[source]))["sources"][0]
    alias_after = shared_source(direct, 39)
    assert canonical_source_id(alias_after) == source["id"]
    assert unit_confirmed(alias_after, "м")
    assert attach(context, record, dict(sources=[alias_after]))["sources"][0] == alias_after


@pytest.mark.parametrize("change", ["binding", "actor", "reason", "key", "unit", "text", "rects", "flags", "unit_flags", "context_hash"])
def test_late_context_does_not_relax_alias_or_unit_binding(change):
    from normative_source_identity import canonical_source_id, shared_source
    context, record, source = fixture()
    confirm(context, record, source, "м", "user", "Контекст таблицы")
    alias = attach(context, record, dict(sources=[shared_source(source, 39)]))["sources"][0]
    if change == "binding":
        alias["confirmed_unit_context"]["binding"]["id"] = "another"
    elif change in {"actor", "reason", "key", "unit"}:
        alias["confirmed_unit_context"][change] = {"actor": "different actor", "reason": "different reason",
            "key": "0" * 64, "unit": "шт"}[change]
    elif change == "context_hash":
        alias["source_alias"]["unit_context_sha256"] = "0" * 64
    elif change == "text":
        alias["text"] += " another column"
    elif change == "rects":
        alias["rects"] = [[.5, .6, .7, .8]]
    elif change == "flags":
        alias["numeric_verified"] = False
    else:
        alias["unit_verified"] = True
    assert canonical_source_id(alias) is None
    assert not unit_confirmed(alias, "м")


@pytest.mark.parametrize("field", ["unit", "actor", "reason", "key", "binding"])
def test_malformed_late_context_is_rejected_without_a_type_error(field):
    from normative_source_identity import canonical_source_id, shared_source
    context, record, source = fixture()
    confirm(context, record, source, "м", "user", "Контекст таблицы")
    alias = attach(context, record, dict(sources=[shared_source(source, 39)]))["sources"][0]
    alias["confirmed_unit_context"][field] = []
    assert canonical_source_id(alias) is None
    assert not unit_confirmed(alias, "м")


def test_content_checked_legacy_shared_alias_acquires_late_unit_context_safely():
    from normative_source_identity import canonical_source_id
    context, record, source = fixture()
    confirm(context, record, source, "м", "user", "Контекст таблицы")
    alias = dict(deepcopy(source), id="shared:39:" + digest(source)[:24], physical_source_id=source["id"])
    frozen = deepcopy(alias)
    resolved = attach(context, record, dict(sources=[alias]))["sources"][0]
    assert resolved["id"] == alias["id"]
    assert canonical_source_id(resolved) == source["id"]
    assert unit_confirmed(resolved, "м")
    assert alias == frozen
