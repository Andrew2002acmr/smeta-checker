"""Bounded evidence acquisition over the frozen matching corpus.

Retrieval never scores expected VOR quantities. Tools expose immutable page,
table and cell identities; the model chooses scope, the server owns evidence.
"""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from copy import deepcopy
from typing import Any, Literal

import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PIPELINE_VERSION = 6
MAX_ROUNDS = 3
MAX_CONTEXT = 58000


def search_evidence(kb, query, facts, *, limit=3):
    """Retrieve definitions AND clauses; Corpus.ALL searches clauses only.

    Document-specific and clause-specific queries stay scoped. For a work-name
    query, interleave the two independently ranked indexes within the budget.
    Neither retrieval nor a resource match proves applicability.
    """
    if re.search(r'(?:ГОСТ|СП|ПНСТ|ОДМ)\s*(?:Р\s*)?\d', query, re.I) or re.fullmatch(r'\d+(?:\.\d+)+', query.strip()):
        return kb.search(query, kind='ALL', facts=facts, limit=limit)
    if re.fullmatch(r'\d{2}-\d{2}-\d{3}-\d{2,3}', query.strip()):
        return kb.search(query, kind='GESN', facts=facts, limit=limit)
    norms = kb.search(query, kind='GESN', facts=facts, limit=limit)
    clauses = kb.search(query, kind='ALL', facts=facts, limit=limit)
    output, seen = [], set()
    for index in range(max(len(norms), len(clauses))):
        for pool in (norms, clauses):
            if index < len(pool):
                hit = pool[index]
                key = (hit.get('entity_kind'), hit['id'])
                if key not in seen:
                    output.append(hit)
                    seen.add(key)
    return output[:limit]


def cell_source_id(version, page, table_id, cell_id):
    import hashlib
    namespace = hashlib.sha256(f'{version}:{page}:{table_id}:{cell_id}'.encode()).hexdigest()[:24]
    return f'agent:p{page}:cell:{namespace}'


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool: Literal['search_project', 'search_tables', 'read_page', 'read_source', 'read_table', 'search_norms', 'sum_cells']
    query: str = Field(default='', max_length=300)
    page: int = Field(default=1, ge=1)
    table_id: str = Field(default='', max_length=200)
    offset: int = Field(default=0, ge=0, le=1000000)
    operand_ids: list[str] = Field(default_factory=list, max_length=12)
    unit: str = Field(default='', max_length=30)
    source_id: str = Field(default='', max_length=200)
    row_ids: list[int] = Field(default_factory=list, max_length=12)
    column_ids: list[int] = Field(default_factory=list, max_length=32)


TOOLS = """
Ты работаешь как агент проверки. Если источников недостаточно, верни requests
и пустые items/parameters; не завершай проверку только потому, что первоначальная
выдержка не содержит таблицу. До 4 requests за шаг:
search_project(query, offset): смысл работы/конструкция/участок, без ожидаемого числа ВОР;
search_tables(query, offset): ведомости объёмов, площади, количества и заголовки таблиц;
offset здесь — курсор страниц результатов (первый 0), следующий — search.next_offset.
Повтор того же запроса и курсора не добавляет доказательств. Если search.truncated=true,
остаются другие кандидаты: уточни запрос или продолжи с next_offset. Выдача не доказывает
отсутствие данных в ПД; даже исчерпанный поиск относится только к подготовленному тексту.
read_page(page, offset): следующие 3500 символов физической страницы;
read_source(source_id, offset): дочитать конкретный источник; его текст не исчезает;
read_table(page, table_id, row_ids, column_ids): точные строки и столбцы с заголовками
и ID ячеек. Каталог tables содержит columns и total_rows. Для общего количества
читай итоговые строки, а не первые строки заголовка. Различай Итог по участку и Всего.
Одинаковое число в колонке другого материала не является основанием.
search_norms(query): нормативный пункт, термин или код.
sum_cells(operand_ids, unit): сумма 2–12 числовых ячеек с одной единицей
из заголовков. operand_ids — source_id, полученные read_table. Сначала
установи одинаковый участок и конструкцию; разные столбцы итогов не складывай.
Ответ JSON включает summary, items, parameters, requests (по умолчанию []).
Поиск не доказывает отсутствия данных. coverage указывает неполный OCR.
Числа связывай с одной конструкцией и участком, заголовком и единицей.
Для суммы перечисли все слагаемые и источники; не подгоняй сумму под ВОР.
Сумму можно вернуть сразу в parameters: operation="sum", operand_source_ids —
точные ID числовых ячеек, value — полученная сумма, unit — единица заголовка.
Программа сама пересчитает и проверит операнды без дополнительного запроса модели.
Кандидат с operation="direct" содержит одну итоговую ячейку: передай её source_id,
operation="direct" и точную цитату с числом, единицей и заголовками из её источника.
Не создавай сумму из одной ячейки и не подставляй единицу, отсутствующую в источнике.
В каждом parameters для quantity/thickness передай scope_state="verified" и
scope_quote — точную короткую цитату того же участка/конструкции/слоя, ВКЛЮЧАЯ
число и единицу. Цитата от другого слоя недопустима. Если принадлежность
не установлена, scope_state="unresolved"; такую величину нельзя сравнивать.
Перед итогом needs_facts по количеству обязательно выполни search_tables,
а обрезанное основание дочитай read_source. remaining_steps учитывает запросы модели.
Окончательный ответ допустим только без requests. На последнем шаге дай
обоснованный вывод либо конкретные недостающие факты. Технический сбой не ошибка ВОР.
row_ids — не более 12, column_ids — не более 32; выбирай итог и нужный материал,
а не всю таблицу. Каждый аспект items должен относиться к одному claim_id
(quantity/unit/presence можно объединить). Не объединяй свою работу и related:*.
В каждом item project_source_ids и normative_source_ids содержат не более 8 ID
каждого типа; предпочтительно 1–4 непосредственно подтверждающих этот вывод.
Разные аспекты разнеси по items; не повторяй весь каталог источников в каждом item.
source_id копируй точно из sources.id или результата инструмента source_id.
Не используй ID нормативного PDF вместо ID конкретной выдержки.
normative_work_owner задаёт работу, для которой выбирается норма и её измеритель:
у расчётного материала это родительская работа, её единица может отличаться от единицы материала.
Для parameter=unit_scale используй только буквальную строку «Измеритель» выбранной
нормы этой работы из источника kind=normative. Единица ресурса и число в ПД не являются
измерителем нормы. Найди эту строку через read_source; при отсутствии не подставляй 1.
Расход материала ищи в ресурсах той же применимой нормы по наименованию, виду и
характеристикам материала, а не по совпадению числа с формулой ВОР. Укажи точную строку
ресурса, его единицу и расход на измеритель нормы в отдельном item с разрешённым
claim_id: derived:rate для текущего материала, related:<index>:rate для дочернего. Размерный расход
т/1000 м2 не является безразмерным coefficient. Найденный, но ещё не проверенный расход
опиши как неподтверждённое основание с источником; не называй его отсутствующим.
"""


def resource_sources(item, prefix, *, evidence_trace=None):
    """Read existing KB resource rows with their own physical-page citations."""
    from normative_resource_consumption import record_candidates,resource_quote
    record_candidates(item,prefix,evidence_trace)
    pages = {}
    for resource in item.get('resources',[])[:80]:
        for citation in resource.get('citations',[])[:1]:
            page = citation.get('page')
            if not page:
                continue
            text = resource_quote(resource)
            pages.setdefault((citation['source_id'],page),[]).append(text)
    return [dict(id=f'{prefix}:resources:{page}',kind='normative',page=page,source_id=source_id,
        label=item['designation']+' · '+item['code']+' · ресурсы',origin=item['extraction_status'],
        text='Ресурсы нормы '+item['code']+'; Измеритель: '+item['unit_raw']+'\n'+'\n'.join(rows))
        for (source_id,page),rows in pages.items()]


def model_result(result, budget=10000):
    """Bound navigation, not storage: full tool results remain in events/sources.

    The source registry and exact row/column addresses allow omitted data to be
    read again. Never turn a truncated search into an absence claim.
    """
    def shrink(value):
        if isinstance(value, dict):
            keys = sorted(value, key=lambda k: (k not in {'rows','source_id','page','table_id','value','operands'},))
            return {k: shrink(value[k]) for k in keys
                if k not in {'coverage','cells','bbox','cell_id','header_ids'}}
        if isinstance(value, list):
            return [shrink(v) for v in value]
        return value
    value = shrink(result)
    # Headers are shared per physical column rather than copied for every cell.
    def bound(value, remaining):
        if isinstance(value, dict):
            output = {}
            for k,v in value.items():
                part = bound(v, max(0, remaining-len(json.dumps(output,ensure_ascii=False))-len(k)-10))
                output[k] = part
            return output
        if isinstance(value, list):
            output = []
            for i,v in enumerate(value):
                part = bound(v, remaining-len(json.dumps(output,ensure_ascii=False))-80)
                if len(json.dumps(output+[part],ensure_ascii=False)) > max(0,remaining-80):
                    output.append(dict(truncated=True, omitted_count=len(value)-i,
                        next_address={k:v[k] for k in ('page','table_id','row','column','source_id') if isinstance(v,dict) and k in v}))
                    break
                output.append(part)
            return output
        if isinstance(value,str) and len(value)>max(160,remaining):
            return value[:max(160,remaining)]+' [truncated; read_source/read_table]'
        return value
    output = bound(value,budget-600)
    if len(json.dumps(output,ensure_ascii=False)) > budget:
        # A very wide object still has retrievable addresses, never a giant
        # string slice that could sever a source ID or fabricate a JSON cell.
        return dict(truncated=True, read_again=True,
            source_ids=[r['source_id'] for r in result.get('rows',[]) if 'source_id' in r][:32])
    return output


def compact(inputs, sources, preferred=(), *, planning=False, measure=None):
    """Exclude bulky untrusted fact graphs, retaining claim identity and state."""
    value = deepcopy(inputs)
    value['matching_checks'] = [
        {k: c[k] for k in ('claim_id', 'kind', 'state', 'required', 'reasons') if k in c}
        for c in value.get('matching_checks', [])]
    value['sources'] = []
    if 'prepared_quantity_tables' in value:
        value['prepared_quantity_tables'] = model_result(value['prepared_quantity_tables'],6000)
    value['pipeline_version'] = PIPELINE_VERSION
    if 'work_scope_graph' in value:
        from normative_context_window import window
        # Reserve the registry catalogue too: pack_sources appends it after
        # windowing, otherwise resumed evidence can overflow before any text.
        value['archived_source_ids']=[s['id'] for s in sources]
        value=window(value,sources,limit=MAX_CONTEXT,measure=measure)
    from normative_context_packing import pack_sources
    return pack_sources(value,sources,preferred,limit=MAX_CONTEXT,planning=planning,measure=measure)


def model_source_aliases(supplied, sources):
    """Short, exact handles prevent copying errors in long hashed cell IDs.

    Handles are bound to this registry, never inferred from the expected value.
    Canonical SHA-based identities remain unchanged in storage and validators.
    """
    to_model = {s['id']:f's{i}' for i,s in enumerate(sources)}
    keys = {'id','source_id','source_ids','project_source_ids','normative_source_ids',
        'operand_source_ids','operand_ids','operands'}
    def visit(value,key=''):
        if isinstance(value,dict):
            return {k:visit(v,k) for k,v in value.items()}
        if isinstance(value,list):
            return [visit(v,key) for v in value]
        return to_model.get(value,value) if isinstance(value,str) and (key in keys or key.endswith(('_source_id','_source_ids'))) else value
    return visit(supplied), {v:k for k,v in to_model.items()}


def restore_source_ids(answer, aliases):
    for item in getattr(answer,'items',[]):
        item.project_source_ids=[aliases.get(s,s) for s in item.project_source_ids]
        item.normative_source_ids=[aliases.get(s,s) for s in item.normative_source_ids]
    for proposal in getattr(answer,'parameters',[]):
        proposal.source_id=aliases.get(proposal.source_id,proposal.source_id)
        proposal.operand_source_ids=[aliases.get(s,s) for s in proposal.operand_source_ids]
    for proposal in getattr(answer,'adjustments',[]):
        proposal.base_source_id=aliases.get(proposal.base_source_id,proposal.base_source_id)
        proposal.adjustment_source_id=aliases.get(proposal.adjustment_source_id,proposal.adjustment_source_id)
    for request in getattr(answer,'requests',[]):
        request.source_id=aliases.get(request.source_id,request.source_id)
        request.operand_ids=[aliases.get(s,s) for s in request.operand_ids]
    return answer


class EvidenceTools:
    def __init__(self, context, record, index, sources, kb, facts):
        self.context, self.record = context, record
        self.sources, self.kb, self.facts = sources, kb, facts
        self.cells = {}
        self.quantity_sums = []
        self.conditional_repair_candidates: list[dict[str,Any]] = []
        self.invalid_project_parameter_kinds: set[str] = set()
        self.index: Any = None
        self.events = []
        self.preferred = []
        self.table_search_done = False
        self.project_search: Any = None
        self.project_search_results = {}
        self.snapshot_refs = []
        self.coverage: dict[str, Any] = {}
        self.blob_store = context.blob_store
        self.filename = 'Подготовленный ПД'
        self.version = None
        try:
            job = context.repository.load_position_job(record['id'], index)
        except KeyError:
            self.coverage = dict(state='snapshot_unavailable', pending_ocr_pages=[], error_pages=[])
            return
        payload = job.payload
        self.filename = payload.get('pdf_name') or self.filename
        pins = record['config'].get('pdf_versions') or {}
        self.version = pins.get(self.filename)
        # Do not attach a prepared corpus to an arbitrary document version.
        if self.version is None and len(set(pins.values())) == 1:
            self.version = next(iter(pins.values()))
        from audit_snapshot import load_snapshot
        self.index = load_snapshot(context.blob_store, payload['snapshot'])
        jobs = context.repository.list_position_jobs(record['id'], include_results=False)
        self.snapshot_refs = [j.payload['snapshot'] for j in reversed(jobs)
            if j.payload.get('pdf_name') == self.filename and j.payload.get('snapshot')
            and (j.payload.get('pdf_versions') or {}).get(self.filename) == self.version]
        if self.snapshot_refs:
            later = load_snapshot(context.blob_store, self.snapshot_refs[0])
            same_extraction = all(later.metadata.get(k) == self.index.metadata.get(k)
                for k in ('pdf_sha256', 'extraction_key', 'ocr_settings_key'))
            if same_extraction and len(later.pending_ocr) < len(self.index.pending_ocr):
                self.index = later
        refreshed = (record['config'].get('normative_corpora') or {}).get(self.version)
        if refreshed:
            latest = load_snapshot(context.blob_store, refreshed)
            if latest.metadata.get('pdf_sha256') != self.index.metadata.get('pdf_sha256'):
                raise ValueError('Prepared normative corpus belongs to another PDF')
            self.index = latest
            self.snapshot_refs.insert(0, refreshed)
        prepared_tables: Any = self.index.tables
        if record['config'].get('document_preparation_version') and (
            self.index.pending_ocr or not self.index.metadata.get('preparation_complete')
        ):
            raise ValueError('Normative checks require a fully prepared document')
        catalogue = [dict(page=p+1, table_id=t['table_id'], rows=t['row_count'], columns=t['col_count'],
            total_rows=sorted({c['row'] for c in t['cells'] if c['text'].strip().casefold().rstrip(':') in {'всего','итог','итого'}}))
            for p, tables in enumerate(prepared_tables or []) for t in tables]
        self.coverage = dict(state='prepared', pages=len(self.index.texts),
            pending_ocr_pages=[p+1 for p in self.index.pending_ocr],
            error_pages=[int(p)+1 for p in self.index.errors],
            available_tables=catalogue[:80], available_table_count=len(catalogue),
            table_catalog_truncated=len(catalogue)>80)

    def _add(self, source):
        context = getattr(self, 'context', None)
        if context is not None and getattr(context.repository, 'supports_analysis_replay', False) is True:
            from normative_confirmed_units import attach
            resolved = attach(context, self.record, dict(sources=[source], evidence_trace=[]))
            source = resolved['sources'][0]
            self.events.extend(e for e in resolved['evidence_trace'] if e not in self.events)
        existing = next((s for s in self.sources if s['id'] == source['id']), None)
        if existing is None:
            if len(self.sources) >= 1024:
                return {'error': 'source_budget_exhausted'}
            self.sources.append(source)
        return {'source_id': source['id']}

    def quantity_candidates(self, query, *, unit='', height_cm=None, section_context='') -> list[dict[str, Any]]:
        """Precompute bounded alternatives; never use the declared VOR amount."""
        if self.index is None or not self.version:
            return []
        from normative_table_candidates import grouped_totals
        from normative_retrieval import terms
        scope_terms = terms(section_context)-terms(query)
        ranked = []
        # Coverage is a bounded MODEL preview, not an internal search index.
        # Drawing frames can fill that preview before quantity schedules at
        # the end of a large PDF. Search every prepared table exactly once.
        for page, tables in enumerate(self.index.tables or []):
            for table in tables:
                captions = [c['text'] for c in table['cells'] if c.get('role')=='header'
                    and c.get('colspan',1)>=table['col_count'] and c.get('text','').strip()]
                for candidate in grouped_totals(table, section_context+' '+query, unit=unit, height_cm=height_cm, include_single=True):
                    scoped_text = ' '.join([*candidate['headers'],*captions])
                    found_terms = terms(scoped_text)
                    abbreviations = re.findall(r'\b([а-я]{3,5})\.',scoped_text.casefold())
                    matched = {term for term in scope_terms if term in found_terms
                        or any(min(len(term),len(found))>=4 and (term.startswith(found) or found.startswith(term)) for found in found_terms)
                        or any(term.startswith(abbreviation) for abbreviation in abbreviations)}
                    # Section context is a bounded extra signal. A common
                    # street caption must not outrank an exact material and
                    # thickness match merely because it has one more word.
                    ranking_score=candidate['score']+min(8,2*len(matched))+2*bool(candidate['unit_verified'])
                    ranked.append(dict(**candidate, page=page+1, table_id=table['table_id'],
                        section_match_terms=sorted(matched),context_headers=captions,ranking_score=ranking_score))
        ranked.sort(key=lambda c: (-c['ranking_score'],not c['unit_verified'],-c['score']))
        for candidate in ranked[:4]:
            result = self.execute(EvidenceRequest(tool='read_table', page=candidate['page'],
                table_id=candidate['table_id'], row_ids=[candidate['row']], column_ids=candidate['columns']))
            candidate['operand_source_ids'] = [c['source_id'] for c in result['rows']
                if 'source_id' in c and c['cell_id'] in candidate['cell_ids']]
            if candidate['operation']=='sum' and candidate['numeric_verified'] and candidate['unit_verified']:
                computed = self.execute(EvidenceRequest(tool='sum_cells',
                    operand_ids=candidate['operand_source_ids'], unit=unit))
                if 'error' not in computed:
                    # This proves arithmetic over this exact physical group,
                    # never its applicability to the current VOR construction.
                    equation = computed['equation'].rsplit('\n',1)[-1].partition(' Ячейки:')[0]
                    candidate.update(source_id=computed['source_id'], equation=equation,
                        calculated_sum=computed['value'], unit=unit)
                    self.preferred.append(computed['source_id'])
                    self.events.append(dict(kind='prepared_quantity_sum',source_id=computed['source_id'],
                        operand_source_ids=candidate['operand_source_ids'],page=candidate['page'],
                        table_id=candidate['table_id'],row=candidate['row'],columns=candidate['columns'],
                        unit=unit,scope_verified=False))
        return ranked[:4]

    def execute(self, request):
        if request.tool == 'read_source':
            source = next((s for s in self.sources if s['id'] == request.source_id), None)
            if source is None:
                return {'error': 'unknown_source'}
            text = source['text']
            self.preferred.append(source['id'])
            if request.offset:
                source = {**source, 'id':source['id']+f':part:{request.offset}', 'text':text[request.offset:request.offset+3500]}
                self.preferred.append(source['id'])
                return self._add(source)
            return {'source_id': source['id'], 'full_length':len(text)}
        if request.tool == 'sum_cells':
            return self._sum(request)
        if request.tool == 'search_norms':
            from normative_service import candidate
            from normative_source_registry import full_text_handle
            results = []
            for hit in search_evidence(self.kb, request.query, self.facts, limit=3):
                item = candidate(self.kb, hit, self.facts)
                if not item['citations']:
                    continue
                ref = item['citations'][0]
                source = dict(id=full_text_handle('agent:n:'+str(item['id']),item['text']), kind='normative',
                    source_id=ref['source_id'], page=ref['page'], label=item['designation']+' · '+item['code'],
                    origin=item['extraction_status'], text=item['text'])
                results.append(self._add(source))
                results.extend(self._add(resource) for resource in resource_sources(item,source['id'],evidence_trace=self.events))
            return {'results': results}
        if self.index is None or not self.version:
            return {'error': 'prepared_corpus_unavailable', 'coverage': self.coverage}
        if request.tool in {'search_project', 'search_tables'}:
            from normative_retrieval import document_search
            self.table_search_done |= request.tool == 'search_tables'
            if getattr(self, 'project_search', None) is None:
                self.project_search = document_search(self.index.texts)
            cache = getattr(self, 'project_search_results', {})
            self.project_search_results = cache
            key = (request.tool, request.query, request.offset)
            if key in cache:
                return {**deepcopy(cache[key]), 'repeated_query': True}
            search = self.project_search.search_page(request.query,
                tables=request.tool == 'search_tables', offset=request.offset)
            pages = []
            for hit in search['hits']:
                pages.append(dict(**self._page(hit['page'], hit['offset']), preview=hit['preview']))
            search = {k: v for k, v in search.items() if k != 'hits'}
            self.events.append(dict(kind='project_retrieval_page_v1', tool=request.tool,
                query=request.query, **search, returned_pages=[p['page'] for p in pages]))
            response = {'pages': pages, 'coverage': self.coverage, 'search': search}
            cache[key] = deepcopy(response)
            return response
        if request.page > len(self.index.texts):
            return {'error': 'page_out_of_range'}
        if request.tool == 'read_page':
            return self._page(request.page, request.offset, exact=True)
        tables = self._tables(request.page)
        table = next((t for t in tables if t['table_id'] == request.table_id), None)
        if not request.table_id and len(tables) == 1:
            table = tables[0]
        if table is None:
            return {'error': 'unknown_table', 'table_ids': [t['table_id'] for t in tables]}
        cells = table['cells']
        selected = [c for c in cells if (c['row'] in request.row_ids if request.row_ids else request.offset <= c['row'] < request.offset+4)
            and (not request.column_ids or c['column'] in request.column_ids)]
        output = []
        for cell in selected:
            header_text = ' | '.join(str(h['text']) for h in cells if h['cell_id'] in cell.get('header_ids', []))
            from pdf_row_sections import row_scope_context
            row_context=row_scope_context(table,cell)
            if row_context:
                header_text+='\n'+row_context
            from pdf_cell_ocr import column_unit
            resolved_unit = column_unit(table, cell)
            if resolved_unit:
                header_text += '\nЕдиница физического столбца: '+resolved_unit
            text = header_text
            row = sorted((c for c in cells if c['row'] == cell['row'] and c['column'] < 2), key=lambda c: c['column'])
            text += '\n' + ' | '.join(str(c['text']) for c in row) + '\n' + cell['text']
            cell_id = cell_source_id(self.version, request.page, table['table_id'], cell['cell_id'])
            self.cells[cell_id] = dict(cell=cell, headers=text, header_text=header_text, page=request.page,unit=resolved_unit,
                version_id=self.version,table_id=table['table_id'])
            geometry = (getattr(self.index, 'metadata', {}).get('page_geometry') or {}).get(str(request.page-1))
            rects = []
            if geometry and cell.get('bbox'):
                from ocr_engine import _apply_matrix
                box = _apply_matrix(cell['bbox'], geometry['matrix'])
                rects = [[max(0,min(1,box[0]/geometry['width'])),max(0,min(1,box[1]/geometry['height'])),
                    max(0,min(1,box[2]/geometry['width'])),max(0,min(1,box[3]/geometry['height']))]]
            output.append(self._add(dict(id=cell_id, kind='project',
                version_id=self.version, page=request.page, label=self.filename,
                text=text, rects=rects, numeric_verified=bool(cell.get('numeric_verified')),
                unit_verified=bool(resolved_unit),unit=resolved_unit,
                origin=(cell.get('origin', 'native') if cell.get('quality', 'usable') == 'usable' else 'uncertain_table'))))
            if request.row_ids and request.column_ids:
                self.preferred.append(cell_id)
        from normative_retrieval import terms
        wanted = terms(request.query)
        matching_rows = sorted({c['row'] for c in cells if wanted & terms(c['text'])})[:20]
        return {'cells': output, 'matching_rows': matching_rows, 'columns': table_outline(table)['columns'], 'rows': [dict(**{k: c[k] for k in ('cell_id', 'row', 'column', 'text', 'header_ids', 'bbox', 'quality', 'numeric_verified','row_scope') if k in c},
            **output[i]) for i,c in enumerate(selected)], 'next_offset': request.offset+4, 'row_count': table['row_count']}

    def _sum(self, request):
        import hashlib
        from normative_parameters import number, measure
        ids = request.operand_ids
        if not 2 <= len(ids) <= 12 or len(set(ids)) != len(ids) or not request.unit:
            return {'error': 'distinct_numeric_operands_and_unit_required'}
        cells = [self.cells.get(i) for i in ids]
        if any(c is None for c in cells):
            return {'error': 'unknown_cell'}
        cells = [c for c in cells if c is not None]
        if any((c['cell'].get('origin') == 'ocr' and not c['cell'].get('numeric_verified'))
                or c['cell'].get('quality', 'usable') != 'usable' for c in cells):
            return {'error': 'uncertain_cells_require_image_verification'}
        values = [number(c['cell']['text']) for c in cells]
        if any(v is None for v in values) or any(not c['unit'] or measure('1',request.unit) != measure('1',c['unit']) for c in cells):
            return {'error': 'numeric_cell_or_header_unit_unresolved'}
        values = [v for v in values if v is not None]
        if len({c['page'] for c in cells}) != 1:
            return {'error': 'cross_page_sum_requires_separate_scope_validation'}
        total = sum(values, Decimal(0))
        context = '\n'.join(dict.fromkeys(c.get('header_text', '') for c in cells))
        text = context + '\n' + ' + '.join(str(v) for v in values) + f' = {total} {request.unit}. Ячейки: ' + ', '.join(ids)
        identity=hashlib.sha256(json.dumps([ids,request.unit],ensure_ascii=False).encode()).hexdigest()[:24]
        result = self._add(dict(id='agent:sum:'+identity, kind='project',
            page=cells[0]['page'], version_id=self.version, label=self.filename, origin='calculated_from_cells', text=text,
            numeric_verified=True,unit_verified=True,unit=request.unit,
            rects=[rect for source in self.sources if source['id'] in ids for rect in source.get('rects', [])][:40]))
        if 'source_id' in result:
            records=getattr(self,'quantity_sums',[])
            if not any(r['source_id']==result['source_id'] for r in records):
                records.append(dict(source_id=result['source_id'],operand_source_ids=list(ids),value=str(total),unit=request.unit))
            self.quantity_sums=records
        return dict(**result, equation=text, value=str(total), operands=ids, scope_verified=False)

    def _page(self, page, offset, *, exact=False):
        assert self.index is not None
        text = self.index.texts[page-1]
        # Retrieve the hit itself, not always the first paragraph of a long page.
        piece = text[offset:offset+3500]
        ref = self._add(dict(id=f'agent:p{page}:{offset}', kind='project', page=page,
            version_id=self.version, label=self.filename, text=piece,
            origin='ocr_unverified' if (self.index.quality.get(page-1) or {}).get('origin') == 'ocr' else 'native'))
        if exact:
            self.preferred.append(ref.get('source_id', ''))
        tables = self._tables(page)
        return dict(**ref, page=page, next_offset=offset+3500 if offset+3500 < len(text) else None,
            tables=[table_outline(t) for t in tables])

    def _tables(self, page):
        """Read shared, SHA-verified prepared pages, never repeat PDF extraction.

        Early positions freeze before later CPU slices finish. Reuse tables
        prepared for sibling positions of the same pinned PDF instead of
        treating the early snapshot's missing table as missing project data.
        """
        tables = self.index.tables[page-1] if self.index.tables else []
        if tables:
            return self._verified_tables(page,tables)
        from audit_snapshot import load_json, load_snapshot
        for ref in self.snapshot_refs:
            if ref.get('snapshot_storage') == 'pages-v1':
                manifest = load_json(self.blob_store, dict(key=ref['snapshot_key'], sha256=ref['snapshot_sha256']), cache_page=True)
                if manifest.get('format') != 'page-snapshot-v1':
                    raise ValueError('Invalid shared page manifest')
                if page > len(manifest['pages']):
                    continue
                prepared = load_json(self.blob_store, manifest['pages'][page-1], cache_page=True)
                tables = prepared.get('tables', [])
            else:
                other = load_snapshot(self.blob_store, ref)
                tables = other.tables[page-1] if other.tables else []
            if tables:
                return self._verified_tables(page,tables)
        return []

    def _verified_tables(self,page,tables):
        if not hasattr(self,'context'):
            return tables
        from normative_table_headers import apply
        return apply(self.context,self.record,self.version,page,tables,self.events)


def table_outline(table):
    """Expose complete column ancestry and total row addresses before a paid read."""
    headers = [c for c in table['cells'] if c.get('role') == 'header' and c['text'].strip()]
    columns = []
    for column in range(table['col_count']):
        path = list(dict.fromkeys(c['text'] for c in headers
            if c['column'] <= column < c['column']+c.get('colspan', 1)
            and c.get('colspan', 1) < table['col_count']))
        columns.append(dict(column=column, headers=path))
    totals = [dict(row=c['row'], label=c['text']) for c in table['cells']
        if c['text'].strip().casefold().rstrip(':') in {'всего','итог','итого'}]
    return dict(table_id=table['table_id'], rows=table['row_count'], column_count=table['col_count'],
        columns=columns, total_rows=sorted({c['row'] for c in totals}), total_labels=totals)


def run(ask, client, credentials, instructions, inputs, schema, trace, stats, tools, *, verify_answer=None):
    """Three calls; one source-only repair for a newly exposed conditional error."""
    from normative_context import ModelProviderResponseError
    started = time.monotonic()
    history = []
    feedback = []
    seen_conditions=set()
    previous_invalid_parameters=set()
    late_repair=False
    from normative_claim_memory import ClaimMemory
    claim_memory=ClaimMemory()
    for step in range(MAX_ROUNDS+1):
        if step==MAX_ROUNDS and not late_repair:
            break
        supplied = deepcopy(inputs)
        bounded_history = [dict(request=e['request'],result=model_result(e['result'],4000)) for e in history]
        supplied.update(coverage={key:value for key,value in tools.coverage.items()
            if key not in {'model_context_packing','model_metadata_sizes'}},
            tool_results=bounded_history, remaining_steps=max(0,MAX_ROUNDS-step-1))
        if feedback:
            supplied['answer_validation'] = feedback
        supplied['conditional_repair_candidates'] = list(getattr(tools,'conditional_repair_candidates',()))
        # Literal anchors must be refreshed AFTER tool reads. Initial candidates
        # cannot describe a construction paragraph discovered in a later round.
        from normative_parameter_anchors import parameter_candidates,thickness_adjustments
        supplied['parameter_candidates'] = parameter_candidates(tools.sources)
        from normative_quantity_anchors import quantity_parameter_candidates
        supplied['quantity_parameter_candidates'] = quantity_parameter_candidates(
            tools.sources,getattr(tools,'cells',{}),getattr(tools,'quantity_sums',()))
        supplied['previous_literal_parameters'] = list(getattr(tools,'parameter_evidence',{}).values())
        supplied['previous_claim_citations'] = claim_memory.context()
        owner_work=(inputs.get('normative_work_owner') or {}).get('work') or inputs.get('position',{}).get('work','')
        supplied['normative_thickness_adjustments'] = thickness_adjustments(owner_work,tools.sources)
        stage = 'normative-evidence-plan' if step == 0 else f'normative-evidence-{step}'
        step_instructions = instructions+TOOLS
        if step == 0 and MAX_ROUNDS > 1:
            # Navigation is a separate bounded task, not a full audit of every
            # preliminary claim before the underlying table has been read.
            step_instructions = TOOLS + '\nСейчас ТОЛЬКО выбери до четырёх точных чтений. Приоритет: проектная конструкция, итоговые строки ведомости с подходящими заголовками columns, затем норматив. Используй row_ids из total_rows и column_ids подходящего материала/единицы. Начало таблицы без чисел не заменяет чтение итогов. Не проверяй всю позицию на этом шаге. summary до 200 символов, items=[], parameters=[], requests. На следующем шаге выполни проверку по полученным данным.'
            step_instructions += '\nvor_layer_group.height — сумма технологических слоёв одной конструкции ВОР в см; выбирай проектный столбец полной толщины, а не требуй отдельный столбец каждой технологической операции.'
            step_instructions += '\nprepared_schedule_candidates — только навигация, а не подтверждённое основание. Проверь предложенные read_requests и выбери нужные чтения; запроси норматив по смыслу работы и условиям. Наличие готовой суммы доказывает только арифметику, а принадлежность конструкции и применимость норм должен установить ты по прочитанным источникам.'
        step_instructions += '\nwork_scope_graph показывает связанные работы и материалы одного участка. same_work_other_scopes — только аналогия поиска: количество с другого участка переносить запрещено. shared_project_facts — буквальные основания соседних работ, а не их вывод; самостоятельно докажи принадлежность текущей конструкции и участка.'
        step_instructions += '\ncontext_window.deferred_sources и full_text_archived означают, что полный текст сохранён в реестре. preview — только навигация, не полный норматив. При необходимости условий выполни read_request/read_source; не объявляй отсутствие нормы из-за переноса текста из контекста.'
        step_instructions += '\nprevious_claim_citations сохраняет ранее проверенные ссылки. Если подтверждаешь тот же аспект по той же норме, сохраняй связь с конструкцией ПД. Ссылка на ресурс машины дополняется определением соответствующей нормы и проектной конструкцией; ресурс не заменяет её область применения. Для выбора сметного способа не требуй буквального повторения машины в ПД, если применимая норма и Методика с её исключениями позволяют такой выбор. Проверяй точный expected_text: идентификация марки/модели, функция класса и пригодность конкретного экземпляра — разные вопросы. Не расширяй проверку имени до доказательства свойств, которых нет в предикате; не подтверждай фактическую пригодность без её основания.'
        if inputs.get('evidence_followup'):
            step_instructions += '\nЭто целевой дополнительный поиск по evidence_followup.actions. Используй сохранённые источники и previous_literal_parameters, читай конкретные недостающие основания, не начинай весь анализ сначала. previous_review_items — предыдущие выводы, не доказательства; финальный ответ должен содержать также сохранённые подтверждённые аспекты с точными ссылками. Повторный OCR запрещён.'
        if step >= MAX_ROUNDS-1:
            step_instructions += '\nЭто ОКОНЧАТЕЛЬНЫЙ шаг: requests=[] обязательно. Не ищи снова. Сохрани найденные факты, точные ID ячеек и параметры; непроверенные аспекты — needs_facts с конкретной причиной, не ошибка ВОР. summary до 600 символов, до 16 items с пояснениями до 350 символов.'
            step_instructions += '\nСохраняй отдельные выводы по собственной геометрии, нормативной применимости и related:* расходу дочернего материала. Не объединяй подтверждённую геометрию с неизвестным коэффициентом ради сокращения числа items; объедини только однородные подтверждённые аспекты или сократи пояснения.'
            step_instructions += '\nДля каждого item — до 8 project_source_ids и до 8 normative_source_ids, предпочтительно 1–4 прямых основания каждого типа. Разделяй разные аспекты; не копируй весь каталог источников.'
        if step==MAX_ROUNDS:
            step_instructions += '\nДополнительное исправление: уточнённый параметр ПД впервые выявил несогласованный выбор нормы. '
            step_instructions += 'Используй только уже прочитанные conditional_repair_candidates; никаких новых поисков. Исправь выбор, только если источники доказывают применимость, иначе needs_facts.'
        # All dynamic anchors, history and coverage are now known. Budget the
        # actual aliased request instead of reserving a fixed 20k beforehand.
        from normative_context_packing import serialize_context
        tools.coverage.setdefault('model_metadata_sizes',[]).append(dict(stage=stage,
            fields={key:len(serialize_context(model_source_aliases({key:value},tools.sources)[0]))
                for key,value in supplied.items() if key!='sources'}))
        supplied = compact(supplied,tools.sources,tools.preferred,planning=step==0,
            measure=lambda value: len(serialize_context(model_source_aliases(value,tools.sources)[0])))
        from normative_embedded_context import packing_record
        packing = packing_record(supplied,tools.sources,stage)
        supplied, aliases = model_source_aliases(supplied,tools.sources)
        encoded = serialize_context(supplied)
        packing.update(input_json_characters=len(encoded),input_json_utf8_bytes=len(encoded.encode()),
            character_limit=MAX_CONTEXT)
        tools.coverage.setdefault('model_context_packing',[]).append(packing)
        step_instructions += '\nСсылки s0, s1 и т.д. — точные handles источников этого запроса. Копируй их без изменения; для суммы используй operand_source_ids подходящего quantity_candidates. Не ищи числа по ВОР и не подставляй ID соседней ячейки. previous_literal_parameters — уже прочитанные буквальные числовые основания. Сохрани их, если не нашёл противоречие конструкции; исправление другого аспекта не стирает найденные слагаемые.'
        step_instructions += '\nquantity_parameter_candidates содержит готовый proposal с буквальным числом, единицей и цитатами: переноси эти поля без переписывания и округления. Метаданные provenance/scope_verified не входят в parameters. scope_state=unresolved и scope_verified=false означают, что программа ещё не проверила инженерную принадлежность. Сам проверь участок, материал, конструкцию и слой; только при доказанной связи установи scope_state=verified, сохранив буквальные цитаты ПД. Для группы слоёв ВОР цитируй полную толщину конструкции ПД, не подставляй в цитату толщину отдельной операции.'
        step_instructions += '\nЕсли у источника указан text_embedded_at и embedded_text_complete=true, полный буквальный текст с тем же source_id уже передан по этому пути в контексте. Короткий sources.text — только навигационный отрывок. Прочитай полный текст и все его условия там; повторное read_source ради того же текста не требуется.'
        try:
            answer = restore_source_ids(ask(client, credentials, step_instructions, supplied, schema, trace, stage, stats),aliases)
        except ValidationError as exc:
            if step >= MAX_ROUNDS-1:
                raise
            feedback = []
            for error in exc.errors(include_input=False)[:8]:
                message = 'Ошибка формата: '+'.'.join(map(str,error['loc']))+' '+error['type']
                limit = (error.get('ctx') or {}).get('max_length')
                if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0:
                    message += f' (max_length={limit})'
                feedback.append(message)
            continue
        except (openai.APIConnectionError,openai.APIStatusError,ModelProviderResponseError) as exc:
            code = exc.code if isinstance(exc, ModelProviderResponseError) else getattr(exc, 'status_code', None)
            retryable=isinstance(exc,openai.APIConnectionError) or code in {408,429,500,502,503,504}
            if not retryable or step>=MAX_ROUNDS-1:
                raise
            stats.setdefault('warnings',[]).append('Временный сбой провайдера; повтор в оставшемся бюджете шагов, без повторного OCR.')
            feedback=['Предыдущий запрос не завершился из-за временного сбоя соединения. Прочитанные источники сохранены; выполни проверку по ним.']
            continue
        requests = getattr(answer, 'requests', [])
        if not requests:
            claim_memory.restore(answer,tools.sources,tools.events)
            feedback = verify_answer(answer) if verify_answer is not None else []
            claim_memory.remember(answer,tools.sources,inputs.get('allowed_claim_ids'))
            candidates: list[dict[str,Any]]=list(getattr(tools,'conditional_repair_candidates',()))
            signatures={json.dumps([c['condition_source_id'],c['condition_quote'],
                sorted(b['code'] for b in c['allowed_base_candidates'])],ensure_ascii=False)
                for c in candidates if c.get('project_fact_confirmed') and c.get('allowed_base_candidates')}
            fresh=signatures-seen_conditions
            eligible=(getattr(tools,'allow_late_repair',True) and step==MAX_ROUNDS-1 and bool(feedback) and bool(fresh)
                and 'thickness' in previous_invalid_parameters
                and 'thickness' not in getattr(tools,'invalid_project_parameter_kinds',set()))
            seen_conditions.update(signatures)
            previous_invalid_parameters=set(getattr(tools,'invalid_project_parameter_kinds',set()))
            if eligible:
                late_repair=True
                tools.events.append(dict(kind='late_conditional_repair',reason='corrected_project_thickness_exposed_new_condition',
                    extra_calls_limit=1,search_allowed=False,condition_count=len(fresh)))
                stats.setdefault('warnings',[]).append('Один дополнительный запрос: исправленная толщина ПД впервые выявила условие выбора нормы; используются только прочитанные источники.')
                continue
            if feedback and step < MAX_ROUNDS-1:
                tools.events.append(dict(kind='answer_validation',issues=feedback))
                continue
            unresolved_quantity = any(v.state == 'needs_facts' and set(v.claim_ids) & {'quantity', 'unit'} for v in getattr(answer, 'items', []))
            if unresolved_quantity and not tools.table_search_done:
                if step >= MAX_ROUNDS-1:
                    raise TimeoutError('Quantity search incomplete: table discovery not performed')
                requests = [EvidenceRequest(tool='search_tables', query=str(inputs.get('position', {}).get('work', ''))[:300])]
            else:
                return answer
        if step >= MAX_ROUNDS-1 and getattr(answer, 'items', []):
            # Preserve useful partial evidence without granting a confirmed
            # verdict while the model still requests additional source reads.
            pending = ', '.join(r.tool for r in requests)
            stats.setdefault('missing_facts', []).append('Не завершён поиск источников: '+pending)
            for item in answer.items:
                if item.state == 'confirmed':
                    item.state = 'needs_facts'
                    item.explanation += ' Поиск связанных источников не завершён.'
            answer.requests = []
            return answer
        if step >= MAX_ROUNDS-1 or time.monotonic()-started > 120:
            raise TimeoutError('Evidence acquisition budget exhausted')
        history = []
        for request in requests:
            result = tools.execute(request)
            if result.get('repeated_query') and result.get('search', {}).get('next_offset') is not None:
                request = request.model_copy(update={'offset': result['search']['next_offset']})
                result = tools.execute(request)
            # Table row text is carried by source IDs; keep the reply bounded.
            history.append(dict(request=request.model_dump(), result=result))
        tools.events.extend(history)
        if history and all(h['result'].get('repeated_query') for h in history):
            raise TimeoutError('Evidence search made no progress: repeated exhausted queries; no further model call')
        stats.setdefault('queries', []).extend(f'{r.tool}: {r.query or r.page}' for r in requests)
    raise RuntimeError('Agent did not finish')
