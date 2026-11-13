"""Explicit user-confirmed units for otherwise readable physical source cells.

No inference from VOR quantities, neighbouring columns or product dimensions.
The original PDF excerpt is retained verbatim; contextual units are separate facts.
"""
from copy import deepcopy

from analysis_replay import digest, put

STAGE = 'confirmed_source_unit'
UNITS = {'м', 'м2', 'м3', 'шт', 'т', 'кг', 'га'}


def binding(source):
    result={key:deepcopy(source.get(key)) for key in
        ('id', 'version_id', 'page', 'text', 'rects', 'origin', 'numeric_verified', 'unit', 'unit_verified')}
    result['id']=source.get('physical_source_id',source['id'])
    return result


def identity(project, source):
    return dict(schema=1, stage=STAGE, project_id=str(project), version_id=source['version_id'],
        source_sha256=digest(binding(source)))


def confirm(context, record, source, unit, actor, reason):
    pins=set((record['config'].get('pdf_versions') or {}).values())
    if (source.get('kind')!='project' or ':cell:' not in source['id'] or source.get('version_id') not in pins
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
        if (source.get('kind')!='project' or ':cell:' not in source.get('physical_source_id',source['id'])
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
        source['confirmed_unit_context']=dict(key=digest(ident),**deepcopy(fact))
        event=dict(kind=STAGE,source_id=source['id'],key=digest(ident),unit=fact['unit'],
            actor=fact['actor'],reason=fact['reason'])
        if event not in output.setdefault('evidence_trace',[]):
            output['evidence_trace'].append(event)
    return output


def unit_confirmed(source, unit):
    fact=source.get('confirmed_unit_context') or {}
    return bool(fact.get('provenance')=='explicit_user_confirmation' and fact.get('unit')==unit
        and fact.get('binding')==binding(source) and fact.get('actor') and fact.get('reason'))
