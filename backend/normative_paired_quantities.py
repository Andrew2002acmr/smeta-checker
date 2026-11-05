"""Literal row envelopes for ordered quantity pairs; no construction approval."""
import hashlib
import json
import math

from audit_measurements import paired_measurements


def source_id(version, page, table_id, unit_cell_id, value_cell_id, *, parent_context=None):
    parts=[version,page,table_id,unit_cell_id,value_cell_id,'paired-row-v1']
    if parent_context is not None:
        parts[-1]='paired-row-v2'
        parts.append(hashlib.sha256(json.dumps(parent_context,sort_keys=True,ensure_ascii=False).encode()).hexdigest())
    identity=json.dumps(parts,ensure_ascii=False)
    return f'agent:p{page}:pairrow:'+hashlib.sha256(identity.encode()).hexdigest()[:24]


def source_fields(table, pair, *, version, page, label, parent_context=None):
    cells=sorted((c for c in table['cells']if c['row']==pair['row']),key=lambda c:c['column'])
    text=' | '.join(c['text']for c in cells)
    native=all(c.get('origin',table.get('origin'))=='native'for c in cells
        if c['cell_id']in {pair['unit_cell_id'],pair['value_cell_id']})
    return dict(id=source_id(version,page,table['table_id'],pair['unit_cell_id'],pair['value_cell_id'],parent_context=parent_context),
        kind='project',version_id=version,page=page,label=label,text=text,literal_text=text,
        origin='native'if native else 'ocr_verified_pair'if pair['recognition_verified']else 'uncertain_table',
        numeric_verified=bool(pair['recognition_verified']),
        **({'paired_parent_context':parent_context}if parent_context is not None else{}),
        rects=[rect for anchor in pair['source_anchors']for rect in anchor['rects']],
        paired_measurement_context=dict(table_id=table['table_id'],row=pair['row'],
            unit_cell_id=pair['unit_cell_id'],value_cell_id=pair['value_cell_id'],
            source_anchors=pair['source_anchors'],
            row_cells=[{key:c.get(key)for key in ('cell_id','row','column','text','bbox','origin','words','paired_ocr',
                *(('colspan',)if parent_context is not None else()))}for c in cells]))


def verified_components(source):
    """Recheck the source envelope before emitting/accepting literal proposals."""
    context=source.get('paired_measurement_context')or{}
    if not context or source.get('kind')!='project' or source.get('origin')not in {'native','ocr_verified_pair'}:
        return []
    version,page=source.get('version_id'),source.get('page')
    cells=context.get('row_cells')or[]
    anchors=context.get('source_anchors')or[]
    if (not version or type(page)is not int or page<1 or len(anchors)!=2
            or not cells or len({c.get('cell_id')for c in cells})!=len(cells)
            or any(c.get('row')!=context.get('row')for c in cells)
            or any(type(c.get('column'))is not int or not isinstance(c.get('text'),str)for c in cells)):
        return []
    text=' | '.join(c['text']for c in sorted(cells,key=lambda c:c['column']))
    if source.get('text')!=text or source.get('literal_text')!=text:
        return []
    if source.get('id')!=source_id(version,page,context.get('table_id'),
            context.get('unit_cell_id'),context.get('value_cell_id'),parent_context=source.get('paired_parent_context')):
        return []
    if source.get('paired_parent_context')is not None:
        from normative_paired_scope import validated_parent
        if validated_parent(source)is None:
            return []
    lookup={c['cell_id']:c for c in cells}
    unit,value=[lookup.get(context.get(key))for key in ('unit_cell_id','value_cell_id')]
    if not unit or not value or unit['column']>=value['column']:
        return []
    if source.get('origin')=='ocr_verified_pair':
        from pdf_paired_ocr import verified_reading
        if not verified_reading(unit,'units')or not verified_reading(value,'values'):
            return []
    for anchor,cell in zip(anchors,(unit,value),strict=True):
        if (anchor.get('state')!='located' or not anchor.get('rects')
                or anchor.get('version_id')!=version or anchor.get('page')!=page
                or anchor.get('table_id')!=context.get('table_id')
                or anchor.get('cell_id')!=cell['cell_id'] or anchor.get('literal_text')!=cell['text']):
            return []
        box=anchor.get('bbox')or[]
        rects=anchor.get('rects')or[]
        if (box!=cell.get('bbox')or len(box)!=4 or not all(type(v)in {int,float}and math.isfinite(v)for v in box)
                or not 0<=box[0]<box[2] or not 0<=box[1]<box[3]
                or any(len(rect)!=4 or not all(type(v)in {int,float}and math.isfinite(v)and 0<=v<=1 for v in rect)
                    or rect[0]>=rect[2]or rect[1]>=rect[3]for rect in rects)):
            return []
    if source.get('rects')!=[rect for a in anchors for rect in a['rects']]:
        return []
    measurements=paired_measurements(value['text'],unit['text'])
    if len({m.unit for m in measurements})!=len(measurements):
        return []
    return [dict(value=str(m.value),unit=m.unit)for m in measurements]


def proposal_matches(proposal, source):
    from normative_parameters import measure,number
    return any(number(proposal.get('value'))==number(component['value'])
        and measure('1',proposal.get('unit',''))==measure('1',component['unit'])
        for component in verified_components(source))
