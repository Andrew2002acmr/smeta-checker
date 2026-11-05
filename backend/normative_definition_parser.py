"""Recover native normative definition scope from physical indentation.

Only bounded definition lists between a printed measure and resource header
are accepted. Parent headings precede their children; they never belong to the
last sibling. No project quantities, norm-code allowlist or model calls.
"""
import re
from typing import Any

VERSION='native-definition-scope-v1'
CODE=r'\d{2}\s*-\s*\d{2}\s*-\s*\d{3}\s*-\s*\d{2,3}'
TABLE=re.compile(r'^Таблица\s+(ГЭСН[мр]?)\s+(\d{2}-\d{2}-\d{3})(?!-)',re.I)


def clean(text):
    return re.sub(r'\s+',' ',text).strip()


def source_rows(document) -> list[dict[str,Any]]:
    """Keep visible native line geometry; tiny hidden bookmark text is not ink."""
    output=[]
    for page in document:
        fragments=[]
        for block in page.get_text('dict')['blocks']:
            for line in block.get('lines',[]):
                if tuple(line.get('dir',(1,0)))!=(1,0):
                    continue
                spans=[s for s in line.get('spans',[]) if s.get('size',0)>=4 and s['text'].strip()]
                if not spans:
                    continue
                text=clean(' '.join(s['text'] for s in spans))
                bbox=[min(s['bbox'][0] for s in spans),min(s['bbox'][1] for s in spans),
                    max(s['bbox'][2] for s in spans),max(s['bbox'][3] for s in spans)]
                if text and 0<=bbox[0]<bbox[2]<=page.rect.width+1 and 0<=bbox[1]<bbox[3]<=page.rect.height+1:
                    fragments.append(dict(text=text,bbox=bbox))
        bands=[]
        for fragment in sorted(fragments,key=lambda f:(f['bbox'][1],f['bbox'][0])):
            center=(fragment['bbox'][1]+fragment['bbox'][3])/2
            band=next((b for b in reversed(bands[-3:]) if abs(b['center']-center)<=2),None)
            if band is None:
                band=dict(center=center,pieces=[])
                bands.append(band)
            if fragment not in band['pieces']:
                band['pieces'].append(fragment)
        for band in bands:
            pieces=sorted(band['pieces'],key=lambda p:p['bbox'][0])
            output.append(dict(page=page.number+1,page_size=[page.rect.width,page.rect.height],text=' '.join(p['text'] for p in pieces),pieces=pieces,
                bbox=[min(p['bbox'][0] for p in pieces),min(p['bbox'][1] for p in pieces),
                    max(p['bbox'][2] for p in pieces),max(p['bbox'][3] for p in pieces)]))
    return output


def _anchor(row) -> dict[str,Any] | None:
    pieces=row.get('pieces',[])
    if not pieces or not re.fullmatch(CODE,pieces[0]['text']):
        return None
    code=re.sub(r'\s+','',pieces[0]['text'])
    remaining=pieces[1:]
    if (not remaining or min(p['bbox'][0] for p in remaining)<=pieces[0]['bbox'][2]+2
            or not any(re.search(r'[а-яa-z\d]',p['text'],re.I) for p in remaining)):
        return None
    return dict(code=code,label=' '.join(p['text'] for p in remaining),
        indent=min(p['bbox'][0] for p in remaining),code_right=pieces[0]['bbox'][2],row=row)


def _running_matter(row):
    return bool(re.match(r'^ГЭСН\s+81',row['text']) or re.fullmatch(r'\d{3,}',row['text'])
        and row.get('page_size') and row['bbox'][1]>.9*row['page_size'][1])


def _operation_codes(text,table_code):
    if not re.match(r'^Для\s+норм',text,re.I):
        return None
    codes=[re.sub(r'\s+','',m[0]) for m in re.finditer(CODE,text)]
    if not codes or any(c.rsplit('-',1)[0]!=table_code for c in codes):
        return set()
    if len(codes)==2 and re.search(r'\bпо\b',text,re.I):
        first,last=(int(c.rsplit('-',1)[1]) for c in codes)
        if not 0<=last-first<=99:
            return set()
        return {table_code+'-'+str(n).zfill(len(codes[0].rsplit('-',1)[1])) for n in range(first,last+1)}
    return set(codes)


def _operations(rows,table_code,norm_code):
    started=False
    current=None
    selected=[]
    for row in rows:
        text=row['text']
        if text.startswith('Состав работ:'):
            started=True
            current='all'
            continue
        if not started:
            continue
        if re.match(r'^(?:ГЭСН\s+81|\d{3,}$)',text):
            continue
        codes=_operation_codes(text,table_code)
        if codes is not None:
            current=codes
            if norm_code in codes:
                selected.append(row)
        elif current=='all' or isinstance(current,set) and norm_code in current:
            if re.match(r'^\d{2}\.\s',text) or selected and row['bbox'][0]>=selected[-1]['bbox'][0]-2:
                selected.append(row)
            else:
                return []  # Unstructured composition is not silently assigned.
    return selected


def parse_rows(rows: list[dict[str,Any]]):
    """Return proved definitions and explicit gaps; do not guess ambiguous lists."""
    output: dict[str,dict[str,Any]]={}
    seen=set()
    gaps=[]
    table: dict[str,Any] | None=None
    history=[]
    for i,row in enumerate(rows):
        heading=TABLE.match(row['text'])
        if heading:
            table=dict(code=heading[2],family=heading[1],title=row['text'][heading.end():].strip(),rows=[row])
            history=[]
            # A wrapped title remains above the composition, in the title column.
            for extra in rows[i+1:i+7]:
                if _running_matter(extra):
                    continue
                if extra['page']!=row['page'] or extra['text'].startswith(('Состав работ:','Измеритель:')):
                    break
                if extra['bbox'][0]<=row['pieces'][0]['bbox'][2]+2 or _anchor(extra):
                    break
                table['title']+=' '+extra['text']
                table['rows'].append(extra)
        if table is None:
            continue
        if not row['text'].startswith('Измеритель:'):
            history.append(row)
            continue
        measure_rows=[row]
        # A list may not cross an unbounded page/resource/table boundary.
        following=[]
        closed=False
        for extra in rows[i+1:]:
            if extra['page']>row['page']+1 or TABLE.match(extra['text']) or extra['text'].startswith('Измеритель:'):
                break
            if _running_matter(extra):
                continue
            if 'Код ресурса' in extra['text']:
                closed=True
                break
            following.append(extra)
        # Explicitly open parentheses in a wrapped unit/range are part of
        # the printed measure, not a new parent heading. Keep every line.
        balance=row['text'].count('(')-row['text'].count(')')
        measure_indent=row['pieces'][-1]['bbox'][0]
        while balance>0 and following:
            extra=following[0]
            if (extra['page']!=row['page'] or abs(extra['bbox'][0]-measure_indent)>2
                    or _anchor(extra) or extra['text'].endswith(':')):
                break
            measure_rows.append(following.pop(0))
            balance+=extra['text'].count('(')-extra['text'].count(')')
        anchors=[_anchor(r) for r in following]
        codes=[a['code'] for a in anchors if a]
        if not codes:
            continue
        duplicates={code for code in codes if code in seen or codes.count(code)>1}
        seen.update(codes)
        for code in duplicates:
            output.pop(code,None)
        label_right=max(a['indent'] for a in anchors if a)
        # Resource column labels often start one visual line above the words
        # "Код ресурса". They consist solely of code fragments well to the
        # right of the definition's code column, not description prose.
        while following and all(re.fullmatch(r'[\d\s-]+',p['text']) and p['bbox'][0]>label_right+2
                for p in following[-1]['pieces']):
            following.pop()
            anchors.pop()
        if (not closed or len(codes)!=len(set(codes)) or any(c.rsplit('-',1)[0]!=table['code'] for c in codes)
                or not table['title'] or duplicates
                or balance!=0
                or any(abs(a['row']['pieces'][0]['bbox'][0]-table['rows'][0]['bbox'][0])>2 for a in anchors if a)):
            gaps.append(dict(table_code=table['code'],codes=codes,page=row['page'],reason='unbounded_or_inconsistent_definition_list'))
            continue
        pending=[]
        parents: list[dict[str,Any]]=[]
        definitions: list[dict[str,Any]]=[]
        previous: dict[str,Any] | None=None
        bad=False
        detail=''
        for current,anchor in zip(following,anchors,strict=True):
            if anchor is None:
                pending.append(current)
                continue
            if pending:
                # A wrapped previous description and the next outdented
                # heading can occur between two codes. Split only at a
                # physical outdent; a colon at the description indent is
                # still ambiguous and must never be silently continued.
                if previous and pending[-1]['text'].endswith(':'):
                    split=next((j for j,r in enumerate(pending) if r['bbox'][0]<previous['indent']-2),None)
                    if split and all(not r['text'].endswith(':') and abs(r['bbox'][0]-previous['indent'])<=2
                            for r in pending[:split]):
                        previous['own_rows'].extend(pending[:split])
                        previous['label']+=' '+' '.join(r['text'] for r in pending[:split])
                        pending=pending[split:]
                indent=pending[0]['bbox'][0]
                is_heading=(pending[-1]['text'].endswith(':')
                    and all(abs(r['bbox'][0]-indent)<=2 for r in pending)
                    and anchor['code_right']+2<indent<anchor['indent']-2
                    and all(not re.fullmatch(CODE,r['pieces'][0]['text']) for r in pending))
                if is_heading:
                    if previous and abs(indent-previous['indent'])<=2:
                        # Equal indentation cannot separate a wrapped previous
                        # label from a new multi-line parent. Assigning all of
                        # it to the next sibling silently truncates the former.
                        bad=True
                        detail='previous_label_and_following_heading_same_indent'
                        break
                    while parents and parents[-1]['indent']>=indent-2:
                        parents.pop()
                    parents.append(dict(indent=indent,child_indent=anchor['indent'],rows=list(pending)))
                elif (not any(r['text'].endswith(':') for r in pending) and previous
                        and all(abs(r['bbox'][0]-previous['indent'])<=2 for r in pending)):
                    previous['own_rows'].extend(pending)
                    previous['label']+=' '+' '.join(r['text'] for r in pending)
                else:
                    bad=True
                    detail='pending_heading_or_continuation_geometry'
                    break
                pending=[]
            while parents and anchor['indent']<parents[-1]['child_indent']-2:
                parents.pop()  # A shallower standalone norm ends the sub-group.
            if parents and abs(anchor['indent']-parents[-1]['child_indent'])>2:
                bad=True
                detail='inconsistent_child_indent'
                break
            previous=dict(anchor,own_rows=[current],parents=[r for p in parents for r in p['rows']])
            definitions.append(previous)
        if pending and not bad:
            # Printed split column codes before the resource header are not
            # definition continuation. Reject rather than append them as prose.
            if previous and all(not r['text'].endswith(':') and abs(r['bbox'][0]-previous['indent'])<=2
                    for r in pending):
                previous['own_rows'].extend(pending)
                previous['label']+=' '+' '.join(r['text'] for r in pending)
            else:
                bad=True
                detail='trailing_heading_or_continuation_geometry'
        if bad:
            gaps.append(dict(table_code=table['code'],codes=codes,page=row['page'],reason='ambiguous_heading_or_continuation',detail=detail))
            continue
        for definition in definitions:
            code=definition['code']
            ops=_operations(history,table['code'],code)
            parents=definition['parents']
            title=' · '.join(v for v in [table['title'],' '.join(r['text'] for r in parents),definition['label']] if v)
            parts=[*table['rows'],*measure_rows,*parents,*definition['own_rows']]
            text='\n'.join(r['text'] for r in parts)
            if ops:
                text+='\nСостав работ:\n'+'\n'.join(r['text'] for r in ops)
            proof_rows=[*definition['own_rows'],*measure_rows,*parents,*ops,*table['rows']]
            if len(text)>6000:
                gaps.append(dict(table_code=table['code'],code=code,page=row['page'],reason='oversized_or_duplicate_definition'))
                output.pop(code,None)
                continue
            output[code]=dict(code=code,table_code=table['code'],family=table['family'],title=title,text=text,
                proof=dict(version=VERSION,rows=[dict(page=r['page'],bbox=r['bbox'],text=r['text']) for r in proof_rows],
                    parents=[r['text'] for r in parents],own_label=definition['label'],
                    code_bbox=definition['row']['pieces'][0]['bbox'],label_indent=definition['indent']))
    return output,gaps


def parse_document(document):
    return parse_rows(source_rows(document))


def definition_context(rows,code):
    """Literal bounded context only; this does not assign a subgroup to a norm."""
    locations=[i for i,r in enumerate(rows) if r.get('pieces') and re.sub(r'\s+','',r['pieces'][0]['text'])==code]
    contexts=[]
    for location in locations:
        measures=[i for i in range(location) if rows[i]['text'].startswith('Измеритель:')]
        if not measures:
            continue
        start=measures[-1]
        if any('Код ресурса' in r['text'] or TABLE.match(r['text']) for r in rows[start+1:location]):
            continue
        finish=None
        for i in range(location+1,len(rows)):
            current=rows[i]
            if current['page']>rows[start]['page']+1 or TABLE.match(current['text']) or current['text'].startswith('Измеритель:'):
                break
            if 'Код ресурса' in current['text']:
                finish=i
                break
        if finish is None:
            continue
        headers=[r for r in rows[:start] if (heading:=TABLE.match(r['text'])) and heading[2]==code.rsplit('-',1)[0]]
        content=[*headers[-1:],*(r for r in rows[start:finish] if not _running_matter(r))]
        contexts.append(dict(text='\n'.join(r['text'] for r in content),
            proof=dict(version=VERSION,kind='context_only',complete_definition_list=True,
                rows=[dict(page=r['page'],bbox=r['bbox'],text=r['text']) for r in content])))
    return contexts[0] if len(contexts)==1 else None
