"""Addressed parent-row observations, never automatic engineering approval."""
import math
import re

VERSION='numbered-paired-parent-v1'
POSITION=re.compile(r'\d+(?:\.\d+)*')


def _cell(cell):
    return {key:cell.get(key)for key in ('cell_id','row','column','colspan','text','bbox','origin')}


def observe(index,version,page,table,pair):
    """Follow one explicit number hierarchy inside one physical table only."""
    cells=table['cells']
    labels=[c for c in cells if c['row']==pair['row']and c['column']<pair['unit_column']
        and re.fullmatch(r'\d+(?:\.\d+)+',c['text'].strip())]
    if len(labels)!=1:
        return None
    label=labels[0]
    prefix=label['text'].strip().rsplit('.',1)[0]
    parents=[c for c in cells if c['column']==label['column']and c['row']<label['row']
        and c.get('colspan',1)==label.get('colspan',1)and c['text'].strip()==prefix]
    if len(parents)!=1:
        return None
    parent=parents[0]
    if any(c['column']==label['column']and parent['row']<c['row']<label['row']
            and POSITION.fullmatch(c['text'].strip())and not c['text'].strip().startswith(prefix+'.')for c in cells):
        return None
    headers={c['cell_id']:c for c in cells if c.get('role')=='header'}
    descriptions=[c for c in cells if c['row']==parent['row']and c.get('role')=='cell'
        and parent['column']<c['column']<pair['unit_column']and c['text'].strip()
        and any(re.search(r'наименован|description',headers[h]['text'],re.I)
            for h in c.get('header_ids',[])if h in headers)]
    if len(descriptions)!=1:
        return None
    from pdf_source_links import source_link
    addressed=[dict(cell=_cell(c),anchor=source_link(index,version,page,table,c))
        for c in (label,parent,descriptions[0])]
    if any(a['anchor']['state']!='located'or (c.get('geometry_validation')or{}).get('requires_source_review')
            or c.get('quality','usable')not in {'usable','uncertain'}
            for a,c in zip(addressed,(label,parent,descriptions[0]),strict=True)):
        return None
    return dict(version=VERSION,scope_verified=False,discovery_only=True,
        binding=dict(version_id=version,page=page,table_id=table['table_id'],row=pair['row'],
            unit_cell_id=pair['unit_cell_id'],value_cell_id=pair['value_cell_id']),
        child=addressed[0],parent=addressed[1],description=addressed[2])


def validated_parent(source):
    context=source.get('paired_parent_context')or{}
    binding=context.get('binding')or{}
    pair=source.get('paired_measurement_context')or{}
    from normative_paired_quantities import source_id
    if source.get('id')!=source_id(source.get('version_id'),source.get('page'),pair.get('table_id'),
            pair.get('unit_cell_id'),pair.get('value_cell_id'),parent_context=context):
        return None
    if (context.get('version')!=VERSION or context.get('scope_verified')is not False
            or context.get('discovery_only')is not True
            or binding!={key:source.get(key)for key in ('version_id','page')}|{
                key:pair.get(key)for key in ('table_id','row','unit_cell_id','value_cell_id')}):
        return None
    addressed=[context.get(key)or{}for key in ('child','parent','description')]
    for item in addressed:
        cell,anchor=item.get('cell')or{},item.get('anchor')or{}
        box=cell.get('bbox')or[]
        if (anchor.get('state')!='located' or not anchor.get('rects')
                or any(anchor.get(key)!=binding.get(key)for key in ('version_id','page','table_id'))
                or any(anchor.get(key)!=cell.get(key)for key in ('cell_id','bbox'))
                or anchor.get('literal_text')!=cell.get('text')
                or type(cell.get('row'))is not int or type(cell.get('column'))is not int
                or len(box)!=4 or not all(type(v)in {int,float}and math.isfinite(v)for v in box)
                or not 0<=box[0]<box[2]or not 0<=box[1]<box[3]):
            return None
        if any(len(rect)!=4 or not all(type(v)in {int,float}and math.isfinite(v)and 0<=v<=1 for v in rect)
                or rect[0]>=rect[2]or rect[1]>=rect[3]for rect in anchor['rects']):
            return None
    child,parent,description=[a['cell']for a in addressed]
    if (child not in [_cell(c)for c in pair.get('row_cells',[])]
            or child['row']!=pair.get('row') or parent['row']>=child['row']
            or parent['row']!=description['row']or parent['column']!=child['column']
            or not parent['column']<description['column']
            or child['text'].strip().rsplit('.',1)[0]!=parent['text'].strip()):
        return None
    return context


def preview(source):
    context=validated_parent(source)
    if context is None:
        return None
    binding=context['binding']
    description=context['description']['cell']
    parent=context['parent']['cell']
    text=description['text']
    return dict(child_position=context['child']['cell']['text'],parent_position=parent['text'],
        text=text[:450],excerpt_truncated=len(text)>450,full_length=len(text),
        scope_verified=False,recognition_verified=description.get('origin')=='native',
        read_request=dict(tool='read_table',page=binding['page'],document_version_id=binding['version_id'],
            table_id=binding['table_id'],row_ids=[parent['row']],column_ids=[parent['column'],description['column']]))


def scope_issue(source,work):
    """Reject explicit contradictions; absence of a conflict proves nothing."""
    if not source.get('paired_measurement_context'):
        return None
    text=source.get('text','')
    wanted={v.replace(',','.')for v in re.findall(r'\bшту\s*(\d+(?:[.,]\d+)+)',work.casefold())}
    found={v.replace(',','.')for v in re.findall(r'\bшту\s*(\d+(?:[.,]\d+)+)',text.casefold())}
    if wanted and found and wanted.isdisjoint(found):
        return 'Обозначение ШТУ в строке ПД отличается от позиции ВОР; совпадение количества не подтверждает материал.'
    parent=validated_parent(source)
    if parent is None:
        return None
    parent_text=parent['description']['cell']['text'].casefold()
    def alignment(value):
        return {kind for pattern,kind in ((r'\bкрив\w*','curve'),(r'\bпрям\w*','straight'))if re.search(pattern,value)}
    expected,observed=alignment(work.casefold()),alignment(parent_text)
    if len(expected)==len(observed)==1 and expected!=observed:
        return 'Количество относится к другой родительской работе: прямой и кривой участки пути нельзя подменять.'
    demolition=r'^\s*(?:разбор\w*|демонт\w*)'
    installation=r'^\s*(?:уклад\w*|устройств\w*|монтаж\w*)'
    if (re.search(demolition,work.casefold())and re.search(installation,parent_text)
            or re.search(installation,work.casefold())and re.search(demolition,parent_text)):
        return 'Родительская строка ПД относится к другой операции: монтаж и демонтаж не взаимозаменяемы.'
    return None
