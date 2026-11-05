"""Source-anchored retrieval benchmark: expected PDF values never enter search."""
from decimal import Decimal
import math
from typing import Any

from pdf_ocr_acceptance import _overlap,normalized
from pdf_cell_ocr import column_unit


def evaluate(index, source_sha256, annotations, discover, *, resolve_source=None):
    if annotations.get('source_sha256')!=source_sha256:
        raise ValueError('Fact annotations belong to another PDF')
    results=[]
    for case in annotations['cases']:
        expected=case['expected']
        if (type(expected.get('page')) is not int or not 1<=expected['page']<=len(index.texts)
                or not expected.get('cells') or not case.get('unit') or not str(case.get('query','')).strip()):
            raise ValueError('Invalid fact annotation')
        boxes=[]
        for cell in expected['cells']:
            box=cell.get('bbox')or[]
            if (len(box)!=4 or not all(type(v) in {int,float} and math.isfinite(v) for v in box)
                    or box[0]>=box[2] or box[1]>=box[3] or box in boxes
                    or normalized(cell.get('value'),'decimal') is None):
                raise ValueError('Invalid expected source cell')
            boxes.append(box)
        # Only the engineer's search description enters the production tools.
        candidates=discover(case['query'],unit=case.get('unit',''),height_cm=case.get('height_cm'),
            section_context=case.get('section_context',''))
        found=[]
        for rank,candidate in enumerate(candidates,1):
            page=candidate['page']
            if not 1<=page<=len(index.texts):
                continue
            table=next((t for t in index.tables[page-1] if t['table_id']==candidate['table_id']),None)
            if table is None:
                continue
            for cell in table['cells']:
                if cell['cell_id'] in candidate['cell_ids']:
                    found.append(dict(page=page,cell=cell,candidate=candidate,rank=rank,
                        physical_unit=column_unit(table,cell)))
        operands=[]
        for expected_cell in expected['cells']:
            matches=[item for item in found if item['page']==expected['page']
                and len(item['cell'].get('bbox')or[])==4
                and _overlap(item['cell']['bbox'],expected_cell['bbox'])>=.9]
            unique={item['cell']['cell_id'] for item in matches}
            result: dict[str,Any]=dict(bbox=expected_cell['bbox'],expected_value=expected_cell['value'],retrieved=len(unique)==1)
            if result['retrieved']:
                actual=min(matches,key=lambda item:item['rank'])
                value=normalized(actual['cell']['text'],'decimal')
                result.update(actual_value=actual['cell']['text'],rank=actual['rank'],cell_id=actual['cell']['cell_id'],
                    literal_matches=value is not None and value==normalized(expected_cell['value'],'decimal'),
                    numeric_verified=bool(actual['cell'].get('numeric_verified') or actual['cell'].get('origin')=='native'),
                    physical_unit=actual['physical_unit'],
                    unit_verified=bool(actual['candidate'].get('unit_verified')) and
                        normalized(actual['physical_unit'],'literal')==normalized(case.get('unit',''),'literal'))
                if resolve_source is not None:
                    from normative_source_addresses import cell_source_id
                    from pdf_source_links import source_link
                    version=index.metadata.get('document_version_id')
                    table=next(t for t in index.tables[actual['page']-1]
                        if t['table_id']==actual['candidate']['table_id'])
                    link=source_link(index,version,actual['page'],table,actual['cell'])
                    sid=cell_source_id(version,actual['page'],table['table_id'],actual['cell']['cell_id'])
                    source=resolve_source(sid) if sid in actual['candidate'].get('operand_source_ids',[]) else None
                    # Recompute the expected physical link independently of
                    # the source registry. The right text on a neighboring
                    # cell (or right cell under another version) must fail.
                    result['source_link_roundtrip']=bool(source and source.get('id')==sid
                        and source.get('version_id')==version and source.get('page')==actual['page']
                        and source.get('literal_text')==actual['cell']['text']
                        and source.get('source_anchor')==link and link['state']=='located'
                        and source.get('rects')==link['rects'])
                    result['source_id']=sid
            operands.append(result)
        retrieved=all(item['retrieved']for item in operands)
        verified=retrieved and all(item.get('literal_matches') and item.get('numeric_verified')
            and item.get('unit_verified') for item in operands)
        exact_ranks=[rank for rank in range(1,len(candidates)+1) if
            len([item for item in found if item['rank']==rank])==len(boxes)
            and all(any(item['rank']==rank and item['page']==expected['page']
                and len(item['cell'].get('bbox')or[])==4 and _overlap(item['cell']['bbox'],box)>=.9 for item in found)for box in boxes)]
        results.append(dict(id=case['id'],query=case['query'],page=expected['page'],
            candidate_count=len(candidates),all_operands_retrieved=retrieved,physical_values_verified=verified,
            exact_group_rank=min(exact_ranks) if exact_ranks else None,
            source_links_verified=(all(item.get('source_link_roundtrip')for item in operands)
                if resolve_source is not None else None),
            engineering_scope_verified=False,operands=operands,candidates=candidates,
            expected_sum=str(sum((Decimal(str(c['value']).replace(',','.'))for c in expected['cells']),Decimal(0)))))
    return dict(cases=results,total=len(results),retrieved=sum(c['all_operands_retrieved']for c in results),
        exact_group_top1=sum(c['exact_group_rank']==1 for c in results),
        physical_values_verified=sum(c['physical_values_verified']for c in results),
        source_links_verified=(sum(bool(c['source_links_verified'])for c in results)if resolve_source is not None else None),
        model_requests=0,ocr_calls=0,expected_values_used_for_search=False,engineering_verdicts_executed=False,
        scope='Retrieval recall and physical-field accuracy; not model selection or engineering applicability')
