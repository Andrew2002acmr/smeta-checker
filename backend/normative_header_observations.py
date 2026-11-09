"""Bound OCR discovery readings independently of literal source quotations."""


def discovery_readings(cell):
    """Only observations pinned to this physical header may aid discovery."""
    if cell.get('role')!='header' or (cell.get('geometry_validation')or{}).get('requires_source_review'):
        return []
    readings=[]
    for key in ('header_refinement','header_line_observation'):
        proof=cell.get(key)or{}
        if (proof.get('header_id')!=cell['cell_id'] or proof.get('bbox')!=cell.get('bbox')
                or not proof.get('image_sha256')):
            continue
        readings.extend(' '.join(w.get('text','')for w in variant.get('words',[]))
            for variant in proof.get('variants',[]))
        for line in proof.get('lines',[]):
            readings.append(' '.join(w.get('text','')for w in line.get('rapid_words',[])))
            readings.extend(' '.join(w.get('text','')for w in variant.get('words',[]))
                for variant in line.get('best_variants',[]))
    return list(dict.fromkeys(text for text in readings if text.strip()))


def addresses(table, selected):
    wanted={h for c in selected for h in [c['cell_id'],*c.get('header_ids',[])]}
    direct={c['cell_id']for c in selected}
    headers=sorted((h for h in table['cells']if h['cell_id']in wanted and h.get('header_line_observation')),
        key=lambda h:(h['cell_id']not in direct,-h['row'],h['column']))
    return [dict(header_id=h['cell_id'],row=h['row'],column=h['column']) for h in headers]


def project(table, selected, *, page, offset=0, limit=2):
    lookup={h['cell_id']:h for h in table['cells']}
    output=[]
    for address in addresses(table,selected):
        header=lookup[address['header_id']]
        observation=header['header_line_observation']
        if observation.get('header_id')!=header['cell_id'] or observation.get('bbox')!=header.get('bbox'):
            continue
        lines=[]
        for line in observation.get('lines',[])[offset:offset+limit]:
            readings=[]
            for engine,words,key in [('RapidOCR',line.get('rapid_words',[]),'confidence'),
                    ('Tesseract',line.get('tesseract_words',[]),'conf')]:
                if words:
                    readings.append(dict(engine=engine,text=' '.join(w['text']for w in words),
                        min_confidence=min(w.get(key,0) for w in words)))
            for variant in line.get('best_variants',[]):
                words=variant.get('words',[])
                if words:
                    readings.append(dict(engine='Tesseract/tessdata_best',scale=variant['scale'],
                        text=' '.join(w['text']for w in words),min_confidence=min(w.get('conf',0)for w in words)))
            lines.append(dict(line=line['order'],readings=readings,recognition_verified=False))
        count=len(observation.get('lines',[]))
        end=min(offset+limit,count)
        output.append(dict(header_id=header['cell_id'],page=page,table_id=table['table_id'],
            row=header['row'],column=header['column'],state=observation.get('state'),
            line_offset=offset,line_count=count,next_line_offset=end if end<count else None,
            read_request=dict(tool='read_table',page=page,table_id=table['table_id'],
                row_ids=[header['row']],column_ids=[header['column']],header_line_offset=end if end<count else 0),
            lines=lines,recognition_verified=False,literal_source_text_unchanged=True))
    return output
