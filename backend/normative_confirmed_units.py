"""Explicit user-confirmed units for otherwise readable physical source cells.

No inference from VOR quantities, neighbouring columns or product dimensions.
The original PDF excerpt is retained verbatim; contextual units are separate facts.
"""
from copy import deepcopy

from analysis_replay import digest, put
from normative_source_identity import canonical_source_id, shared_source

STAGE = 'confirmed_source_unit'
UNITS = {'м', 'м2', 'м3', 'шт', 'т', 'кг', 'га'}


def binding(source):
    canonical = canonical_source_id(source)
    if canonical is None:
        return None
    result={key:deepcopy(source.get(key)) for key in
        ('id', 'version_id', 'page', 'text', 'rects', 'origin', 'numeric_verified', 'unit', 'unit_verified')}
    result['id']=canonical
    return result


def identity(project, source):
    bound = binding(source)
    if bound is None:
        raise ValueError('Unit confirmation requires a valid canonical source identity')
    return dict(schema=1, stage=STAGE, project_id=str(project), version_id=source['version_id'],
        source_sha256=digest(bound))


def confirm(context, record, source, unit, actor, reason):
    pins=set((record['config'].get('pdf_versions') or {}).values())
    canonical = canonical_source_id(source)
    if (canonical is None or source.get('kind')!='project' or ':cell:' not in canonical or source.get('version_id') not in pins
            or source.get('numeric_verified') is not True or source.get('unit_verified')
            or unit not in UNITS or not actor.strip() or not reason.strip()):
        raise ValueError('Unit confirmation requires a pinned verified cell, actor and reason')
    fact=dict(schema=1, binding=binding(source), unit=unit, actor=actor, reason=reason,
        provenance='explicit_user_confirmation')
    ident=identity(record['project_id'],source)
    saved=put(context,ident,fact)
    if saved!=fact:
        raise ValueError('A different confirmation already exists; retain history and resolve explicitly')
    return dict(key=digest(ident),fact=saved)


def attach(context, record, result):
    """Attach verified contextual facts without changing literal source text."""
    output=deepcopy(result)
    pins=set((record['config'].get('pdf_versions') or {}).values())
    for source in output.get('sources',[]):
        canonical = canonical_source_id(source)
        if (canonical is None or source.get('kind')!='project' or ':cell:' not in canonical
                or source.get('version_id') not in pins or source.get('numeric_verified') is not True
                or source.get('unit_verified')):
            continue
        ident=identity(record['project_id'],source)
        artifact=context.repository.analysis_artifact(record['project_id'],digest(ident))
        if artifact is None:
            continue
        fact=artifact['payload']
        if (artifact['identity']!=ident or fact.get('binding')!=binding(source)
                or fact.get('unit') not in UNITS or fact.get('provenance')!='explicit_user_confirmation'):
            raise ValueError('Confirmed source unit does not match current evidence')
        previous_context = source.get('confirmed_unit_context')
        if previous_context is None and canonical != source['id'] and 'source_alias' not in source:
            # A content-checked legacy shared alias may acquire the new server
            # attestation without changing its handle or document envelope.
            upgraded = shared_source(source, int(source['id'].split(':', 2)[1]))
            if upgraded is None or upgraded['id'] != source['id']:
                raise ValueError('Confirmed source unit alias cannot retain its identity')
            source['source_alias'] = upgraded['source_alias']
        source['confirmed_unit_context']=dict(key=digest(ident),**deepcopy(fact))
        if 'source_alias' in source and previous_context is None:
            source['source_alias']['unit_context_sha256']=digest(source['confirmed_unit_context'])
        event=dict(kind=STAGE,source_id=source['id'],key=digest(ident),unit=fact['unit'],
            actor=fact['actor'],reason=fact['reason'])
        if event not in output.setdefault('evidence_trace',[]):
            output['evidence_trace'].append(event)
    return output


def unit_confirmed(source, unit):
    fact=source.get('confirmed_unit_context') or {}
    bound = binding(source)
    return bool(bound is not None and fact.get('provenance')=='explicit_user_confirmation' and fact.get('unit')==unit
        and fact.get('binding')==bound and fact.get('actor') and fact.get('reason'))
