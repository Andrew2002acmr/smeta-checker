"""Ordered measurements from physical same-row unit/value cells, not VOR amounts."""
import re

from audit_measurements import paired_measurements

VERSION='physical-row-pairs-v1'


def _column_header(table, cell, pattern):
    """A physical column may span frame/stamp subdivisions of the page grid."""
    lookup={c['cell_id']:c for c in table['cells']}
    left=cell['column']
    right=left+cell.get('colspan',1)
    headers=[lookup[h]for h in cell.get('header_ids',[])if h in lookup
        and lookup[h].get('role')=='header'
        and re.search(pattern,lookup[h].get('text','').replace('\n',' '),re.I)]
    for header in headers:
        if (header['column']!=left or header['column']+header.get('colspan',1)!=right):
            continue
        # Never treat a value crossing two independently labelled subcolumns
        # as one quantity, even when their common heading says Quantity.
        if any(h.get('role')=='header' and header['row']<h['row']<cell['row']
                and left<=h['column']<h['column']+h.get('colspan',1)<=right
                and h.get('colspan',1)<cell.get('colspan',1) for h in table['cells']):
            continue
        return True
    return False


def row_candidates(table, selected):
    """Locate cells before trusting their initial OCR spelling or confidence."""
    from pdf_paired_ocr import literal
    lookup={c['cell_id']:c for c in table['cells']}
    output=[]
    for value in selected:
        if value.get('role')!='cell':
            continue
        if not _column_header(table,value,r'количеств|quantity'):
            continue
        if literal(value['text'],'values') is None:
            continue
        unit_cells=[]
        for unit in table['cells']:
            if (unit.get('role')!='cell' or unit['row']!=value['row']
                    or unit['column']+unit.get('colspan',1)>value['column']
                    or unit.get('rowspan',1)!=value.get('rowspan',1)):
                continue
            if any(re.search(r'единиц.{0,20}измер|\bunit\b',lookup[h].get('text','').replace('\n',' '),re.I)
                    for h in unit.get('header_ids',[])if h in lookup):
                unit_cells.append(unit)
        if len(unit_cells)!=1:
            continue
        unit=unit_cells[0]
        if not _column_header(table,unit,r'единиц.{0,20}измер|\bunit\b'):
            continue
        if value.get('rowspan',1)>1:
            # Frame subdivisions can split the logical row axis too. Accept
            # only when one complete description cell spans the same rows;
            # two separate work descriptions must never share one pair.
            descriptions=[c for c in table['cells']if c.get('role')=='cell'
                and c['column']<unit['column'] and c.get('text','').strip()
                and value['row']<=c['row']<value['row']+value['rowspan']
                and any(re.search(r'наименован|description',lookup[h].get('text',''),re.I)
                    for h in c.get('header_ids',[])if h in lookup)]
            if (len(descriptions)!=1 or descriptions[0]['row']!=value['row']
                    or descriptions[0].get('rowspan',1)!=value['rowspan']):
                continue
        if any((c.get('geometry_validation')or{}).get('requires_source_review')
                or c.get('quality','usable')not in {'usable','uncertain'}
                or c.get('reasons')for c in (value,unit)):
            continue
        output.append((unit,value))
    return output


def observations(table, selected):
    """Return literal candidates; only independent readings can verify OCR."""
    output=[]
    for unit,value in row_candidates(table,selected):
        measurements=paired_measurements(value['text'],unit['text'])
        # Same dimension twice, e.g. шт/шт, has no unique unit selection.
        if not measurements or len({m.unit for m in measurements})!=len(measurements):
            continue
        native=all(c.get('origin',table.get('origin'))=='native'for c in (value,unit))
        if native and any(c.get('quality','usable')!='usable'for c in (value,unit)):
            continue
        pair=dict(version=VERSION,row=value['row'],value_cell_id=value['cell_id'],
            unit_cell_id=unit['cell_id'],value_column=value['column'],unit_column=unit['column'],
            raw_values=value['text'],raw_units=unit['text'],
            components=[dict(index=i,value=str(m.value),unit=m.unit,dimension=m.dimension)
                for i,m in enumerate(measurements)],recognition_verified=native,
            recognition_state='native_literal'if native else 'paired_ocr_requires_verification',
            discovery_only=True,scope_verified=False)
        if not native:
            from pdf_paired_ocr import verified_pair
            if verified_pair(table,pair):
                pair.update(recognition_verified=True,recognition_state='cross_engine_paired_cells')
        output.append(pair)
    return output
