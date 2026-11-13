"""Audit whole raster pages and durable pair evidence without VOR/model input.

Annotations are loaded only after extraction. Recognition correctness and
engineering applicability remain separate from completion of OCR attempts.
"""
import argparse
import hashlib
import json
from pathlib import Path
from typing import Any,cast

import pymupdf

from audit_core import PageTextIndex
from audit_snapshot import load_snapshot, store_snapshot
from normative_agent import EvidenceRequest, EvidenceTools
from normative_paired_quantities import verified_components
from pdf_geometry_contract import validate_tables
from pdf_paired_ocr import complete, pending_cells
from pdf_row_measurements import observations, row_candidates
from pdf_tables import raster_tables
from storage_blobs import FileBlobStore


def inspect_page(original, physical, blobs, *, seconds=90):
    with pymupdf.open() as scan:
        page=scan.new_page(width=original.rect.width,height=original.rect.height)
        page.insert_image(page.rect,stream=original.get_pixmap(dpi=300,alpha=False).tobytes('png'))
        if str(page.get_text()).strip():
            raise ValueError('Raster audit must not have a native text layer')
        tables=cast(list[dict[str,Any]],raster_tables(page))
        validate_tables(page,tables)
        preparation=complete(page,tables,store_image=blobs.put,seconds=seconds,max_cells=128)
        index=PageTextIndex(('',),tables=(tables,),metadata=dict(
            document_version_id=f'raster-page-{physical}',original_page=physical,
            preparation_complete=not bool(preparation['remaining']),
            page_geometry={'0':dict(width=page.rect.width,height=page.rect.height,
                matrix=[page.rotation_matrix[i]for i in range(6)])}))
        ref=store_snapshot(blobs,index)
        restored=load_snapshot(blobs,ref)
        for table in restored.tables[0]:
            for cell in table['cells']:
                proof=cell.get('paired_ocr')or{}
                if proof.get('image_key'):
                    if hashlib.sha256(blobs.get(proof['image_key'])).hexdigest()!=proof['image_sha256']:
                        raise ValueError('Stored paired crop hash mismatch')
                for variant in proof.get('variants',[]):
                    if variant.get('transformed_image_key')and hashlib.sha256(
                            blobs.get(variant['transformed_image_key'])).hexdigest()!=variant['transformed_image_sha256']:
                        raise ValueError('Stored transformed paired crop hash mismatch')
        # Call the actual resume path after durable snapshot roundtrip.
        before=json.dumps(restored.to_snapshot(),sort_keys=True)
        resumed=complete(page,restored.tables[0],store_image=blobs.put,seconds=seconds,max_cells=128)
        if not preparation['remaining']and (resumed['attempted'] or before!=json.dumps(restored.to_snapshot(),sort_keys=True)):
            raise ValueError('Completed paired OCR was repeated during resume')
        tools=EvidenceTools.from_prepared(restored,f'raster-page-{physical}')
        transport=[]
        for table in restored.tables[0]:
            for unit,value in row_candidates(table,table['cells']):
                reply=tools.execute(EvidenceRequest(tool='read_table',page=1,table_id=table['table_id'],
                    row_ids=[value['row']],column_ids=[unit['column'],value['column']]))
                for pair in reply['row_measurements']:
                    source=next(s for s in tools.sources if s['id']==pair['source_id'])
                    components=verified_components(source)
                    if not pair['recognition_verified']and components:
                        raise ValueError('Unverified pair became a literal quantity proposal')
                    if pair['recognition_verified']and len(components)!=len(pair['components']):
                        raise ValueError('Verified pair lost components during tool transport')
                    transport.append(dict(row=value['row'],table_id=table['table_id'],
                        recognition_verified=pair['recognition_verified'],scope_verified=pair['scope_verified'],
                        source_id=pair['source_id'],components=components))
        return restored,dict(page=physical,tables=len(tables),
            candidate_rows=sum(len(row_candidates(t,t['cells']))for t in tables),
            literal_pairs=sum(len(observations(t,t['cells']))for t in tables),
            verified_pairs=sum(p['recognition_verified']for p in transport),
            preparation=preparation,snapshot=ref,resume_attempted=resumed['attempted'],
            remaining={t['table_id']:[c['cell_id']for c,_ in pending_cells(t)]for t in tables if pending_cells(t)},
            tool_transport=transport)


def evaluate(index, cases):
    results=[]
    for case in cases:
        rows=[(table,cell['row'])for table in index.tables[0]for cell in table['cells']
            if cell['text'].strip()==case['printed_row']]
        item=dict(printed_row=case['printed_row'],located=len(rows)==1,literal_matches=False,recognition_verified=False)
        if len(rows)==1:
            table,row=rows[0]
            pairs=[p for p in observations(table,table['cells'])if p['row']==row]
            if len(pairs)==1:
                pair=pairs[0]
                item.update(literal_matches=[c['value']for c in pair['components']]==case['expected_values']
                    and [c['unit']for c in pair['components']]==case['expected_units'],
                    recognition_verified=pair['recognition_verified'])
        results.append(item)
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--pages',type=int,nargs='+',required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--annotations',type=Path)
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    raw=args.pdf.read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    blobs=FileBlobStore(args.out/'blobs')
    reports=[]
    indices={}
    with pymupdf.open(stream=raw,filetype='pdf')as pdf:
        for page in args.pages:
            if not 1<=page<=len(pdf):
                raise ValueError('Invalid physical page')
            index,report=inspect_page(pdf[page-1],page,blobs)
            indices[page]=index
            reports.append(report)
            print(json.dumps(report,ensure_ascii=False),flush=True)
    # Expected values never reach recognition, candidate selection or ranking.
    annotations=json.loads(args.annotations.read_text(encoding='utf-8-sig'))if args.annotations else None
    if annotations:
        if annotations['source_sha256']!=sha:
            raise ValueError('Annotations belong to another source PDF')
        for report in reports:
            report['evaluation']=evaluate(indices[report['page']],
                [c for c in annotations['cases']if c['page']==report['page']])
    result=dict(source_sha256=sha,pages=reports,model_requests=0,native_text_used=False,
        expected_values_used_for_recognition=False,engineering_scope_verified=False)
    (args.out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
