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
from normative_source_addresses import cell_source_id as cell_source_id

PIPELINE_VERSION = 27
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


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool: Literal['search_project', 'search_tables', 'read_page', 'read_source', 'read_table', 'search_norms', 'sum_cells']
    document_version_id: str = Field(default='',max_length=80)
    query: str = Field(default='', max_length=300)
    page: int = Field(default=1, ge=1)
    header_line_offset: int = Field(default=0,ge=0,le=10000)
    table_id: str = Field(default='', max_length=200)
    offset: int = Field(default=0, ge=0, le=1000000)
    operand_ids: list[str] = Field(default_factory=list, max_length=12)
    unit: str = Field(default='', max_length=30)
    source_id: str = Field(default='', max_length=200)
    entity_id: str = Field(default='', max_length=160)
    entity_kind: Literal['', 'ProjectArea', 'Construction', 'Work', 'Material', 'Fact', 'Evidence'] = ''
    entity_offset: int = Field(default=0, ge=0, le=1000000)
    bbox: list[float] = Field(default_factory=list, max_length=4)
    scale: float = Field(default=1, ge=0.25, le=3)
    row_ids: list[int] = Field(default_factory=list, max_length=12)
    column_ids: list[int] = Field(default_factory=list, max_length=32)


TOOLS = """
expert_laying_width — отдельное экспертное основание принятой ширины прохода
для точных закреплённых работ текущей ВОР. Это не цитата ПД и не исполнительный
замер. Его можно использовать только для условия ширины выбранной нормы;
толщину, площадь, принадлежность конструкции и расход доказывай отдельно.
confirmed_unit_context — отдельное явное экспертное подтверждение единицы именно
этой физической ячейки, привязанное к её исходному тексту и версии ПД. Оно не
добавляет единицу в буквальный текст и не подтверждает принадлежность ячейки
конструкции. При наличии такого основания используй его unit; не объявляй
единицу неизвестной только из-за отсутствующего обозначения в заголовке.
Если coverage.supporting_documents непустой, позиция ссылается на несколько PDF.
search_project/search_tables ищут во всех явно привязанных документах. У найденных
страниц указан document_version_id: передавай его в read_page/read_table. Без него
читается основной PDF. Одинаковые номера страниц разных PDF не равны. ПД задаёт
конструкцию/объём, ПОС может подтверждать условия и организацию работ.
Ты работаешь как агент проверки. Если источников недостаточно, верни requests
и пустые items/parameters; не завершай проверку только потому, что первоначальная
выдержка не содержит таблицу. До 4 requests за шаг:
search_project(query, offset): смысл работы/конструкция/участок, без ожидаемого числа ВОР;
search_tables(query, offset): ведомости объёмов, площади, количества и заголовки таблиц;
matched_table_observations содержит адреса ячеек и варианты чтения OCR для поиска.
Это кандидаты, не подтверждённые факты: observation_kind=ocr_alternative/ocr_line_candidate нельзя
цитировать как буквальный текст ПД. Открой таблицу по page/table_id/row/column.
Это ограниченный просмотр: при matched_table_observations_truncated дочитай нужную
таблицу. Варианты одной физической ячейки находятся в readings и не являются
дополнительными независимыми источниками.
read_table возвращает header_observations отдельно от буквального текста.
Если next_line_offset задан, продолжи по read_request/header_line_offset.
recognition_verified=false — наблюдение для поиска, а не подтверждённая цитата.
У prepared_quantity_tables поле discovery_only=true означает только кандидат.
parameter_mismatches требует отдельной проверки преобразования по нормативам;
совпадение суммы само по себе не подтверждает толщину и область применения.
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
Если количество или расход проверяются по рабочей норме с несколькими условными ветвями, сопоставь роль, материал
и назначение проектного слоя с конкретной ветвью источника. Одного совпадения
толщины или числа технологических операций недостаточно. Если связь не доказана,
оставь применимость этой рабочей нормы needs_facts; для материала оцени его собственный
предмет normative:applicability и сохрани независимую геометрию.
Если расход материала взят из рабочей нормы, ищи его в ресурсах этой применимой нормы по наименованию, виду и
характеристикам материала, а не по совпадению числа с формулой ВОР. Укажи точную строку
ресурса, его единицу и расход на измеритель нормы в отдельном item с разрешённым
claim_id: derived:rate для текущего материала, related:<index>:rate для дочернего. Размерный расход
т/1000 м2 не является безразмерным coefficient. Найденный, но ещё не проверенный расход
опиши как неподтверждённое основание с источником; не называй его отсутствующим.
"""


from normative_verification_contract import NORMATIVE_SCOPE_INSTRUCTIONS, UNIT_SCALE_INSTRUCTIONS

TOOLS += NORMATIVE_SCOPE_INSTRUCTIONS + UNIT_SCALE_INSTRUCTIONS
TOOLS += ('\nread_table.row_measurements связывает составные значения и единицы одной физической строки '
    'по порядку components.index: например шт./т и 114/23,53 означают 114 шт и 23,53 т, а не деление. '
    'raw_values/raw_units остаются буквальными, source_anchors и read_request адресуют обе ячейки. '
    'Это не доказательство принадлежности позиции ВОР. При recognition_verified=false чтение OCR ещё не проверено; '
    'не превращай его в подтверждение. Расход вроде кг/м2 не является парой альтернативных измерителей.')
TOOLS += ('\nrow_measurements.measurement_kind=native_scalar_row — отдельное буквальное количество '
    'из нативной строки с явными физическими заголовками единицы и количества. value/unit связаны '
    'с адресами обеих ячеек; это не OCR и не подтверждение инженерной принадлежности. '
    'Используй готовый quantity_parameter_candidates.proposal и отдельно докажи scope.')
TOOLS += ('\nsearch_project также возвращает engineering_entities: краткие сущности с устойчивыми ID. '
    'search_project(query,entity_kind="Construction"|"Work"|"Material"|"Fact"|"ProjectArea"|"Evidence") '
    'ищет только каталог сущностей, без страниц PDF. '
    'Для следующих сущностей используй entity_offset из next_offset этого каталога. '
    'read_source(entity_id, offset,entity_kind) возвращает связанные работы, участки, материалы, факты и доказательства; '
    'entity_kind="Material"/"Fact"/"Work"/"ProjectArea"/"Evidence" выбирает только нужный тип до пагинации. '
    'next_offset дочитывает отношения с тем же фильтром. candidate/rejected/stale не являются verified. '
    'path_state=unresolved означает, что хотя бы одна связь всего пути ещё не подтверждена; '
    'отдельная verified связь не подтверждает принадлежность всей конструкции. '
    'По read_request дочитай физический источник; граф не заменяет проверку принадлежности. '
    'read_page(document_version_id,page,bbox=[x0,y0,x1,y1],scale) получает локальный фрагмент '
    'чертежа в координатах PDF без поворота. Это только изображение, без автоматического распознавания; '
    'requires_visual_reader=true требует отдельной визуальной проверки. Не подтверждай величину по факту получения изображения.')
TOOLS += ('\nЕсли ответ инструмента усечён, archive_read_request дочитывает полный JSON по '
    'result_handle через read_source и next_offset. tool-result:* — навигационный архив, '
    'не источник ПД и не доказательство. Для заключений цитируй только физические sources.id, найденные внутри архива.')


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
            keys = sorted(value, key=lambda k: (k not in {'rows','source_id','page','table_id','value','operands',
                'discovery_only','parameter_mismatches','scope_verified','recognition_verified','header_observations',
                'parameter_match_state','missing_parameters','header_id','read_request','next_line_offset','row_measurements'},))
            return {k: shrink(value[k]) for k in keys
                if k not in {'coverage','cells','bbox','cell_id','header_ids'}}
        if isinstance(value, list):
            return [shrink(v) for v in value]
        return value
    value = shrink(result)
    if len(json.dumps(value, ensure_ascii=False)) <= budget:
        return value
    if 'engineering_entities' in value:
        # Keep a complete cursor page together. Slicing items while retaining
        # its original next_offset silently skips entities on the next call.
        catalogue = {k: value[k] for k in ('engineering_entities', 'result_handle', 'archive_read_request') if k in value}
        catalogue['page_results_archived'] = True
        if len(json.dumps(catalogue, ensure_ascii=False)) <= budget:
            return catalogue
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
            **{k: result[k] for k in ('result_handle', 'archive_read_request') if k in result},
            source_ids=[r['source_id'] for r in result.get('rows',[]) if 'source_id' in r][:32])
    return output


def compact(inputs, sources, preferred=(), *, planning=False, measure=None, read_source_ids=()):
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
        value=window(value,sources,limit=MAX_CONTEXT,measure=measure,read_source_ids=read_source_ids)
    from normative_context_packing import pack_sources
    return pack_sources(value,sources,preferred,limit=MAX_CONTEXT,planning=planning,measure=measure,
        read_source_ids=read_source_ids)


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


def restore_source_ids(answer, aliases, sources=()):
    # Prose aliases belong to this exact dispatch, too. A resumed review may
    # reorder its registry; resolving s65 later against that list mislabels PD
    # as a normative source while structured citation IDs remain correct.
    lookup={s['id']:s for s in sources}
    def prose(text):
        def replace(match):
            source=lookup.get(aliases.get(match[0]))
            if source is None:
                return match[0]
            label=('ПД' if source.get('kind')=='project' and source.get('origin')!='explicit_expert_method_basis'
                else source.get('label','Норматив'))
            return label+(f", стр. {source['page']}" if source.get('page') else '')
        return re.sub(r'(?<!\w)s\d+(?!\w)',replace,text)
    answer.summary=prose(answer.summary)
    for item in getattr(answer,'items',[]):
        item.explanation=prose(item.explanation)
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
    def _reset(self, sources, kb, facts):
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
        self.engineering_graph = None
        self.result_archive = {}
        self.coverage: dict[str, Any] = {}
        self.filename = 'Подготовленный ПД'
        self.version = None

    @classmethod
    def from_prepared(cls, index, version_id, *, filename='Подготовленный ПД'):
        """Source-only tools: no VOR job, provider, OCR or saved verdict needed."""
        version_id=str(version_id)
        if index.metadata.get('document_version_id') != version_id:
            raise ValueError('Prepared snapshot belongs to another document version')
        tools=cls.__new__(cls)
        tools._reset([], None, {})
        tools.index,tools.version,tools.filename=index,version_id,filename
        tools.coverage=dict(state='partial' if index.pending_ocr or index.errors
            or not index.metadata.get('preparation_complete') else 'prepared',
            pages=len(index.texts),pending_ocr_pages=[p+1 for p in index.pending_ocr],
            error_pages=[int(p)+1 for p in index.errors],source_only=True)
        return tools

    def __init__(self, context, record, index, sources, kb, facts):
        self._reset(sources, kb, facts)
        self.context, self.record = context, record
        self.blob_store = context.blob_store
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
        # A fully prepared PDF is normally shared by every position. Preserve
        # newest-first fallback order without rereading an identical manifest
        # once per position on every page that has no tables.
        for sibling in reversed(jobs):
            candidate = sibling.payload.get('snapshot')
            if (candidate and sibling.payload.get('pdf_name') == self.filename
                    and (sibling.payload.get('pdf_versions') or {}).get(self.filename) == self.version
                    and candidate not in self.snapshot_refs):
                self.snapshot_refs.append(candidate)
        if self.snapshot_refs and self.snapshot_refs[0] != payload['snapshot']:
            later = load_snapshot(context.blob_store, self.snapshot_refs[0])
            same_extraction = all(later.metadata.get(k) == self.index.metadata.get(k)
                for k in ('pdf_sha256', 'extraction_key', 'ocr_settings_key'))
            if same_extraction and len(later.pending_ocr) < len(self.index.pending_ocr):
                self.index = later
        refreshed = (record['config'].get('normative_corpora') or {}).get(self.version)
        if refreshed:
            latest = load_snapshot(context.blob_store, refreshed)
            if latest.metadata.get('pdf_sha256') != self.index.metadata.get('pdf_sha256'):
                from normative_preparation_lineage import transition
                self.events.append(transition(context,record,payload,self.filename,self.version,
                    self.index,latest,refreshed))
                # Cross-contract fallback must not reintroduce old tables on
                # a page where the new producer recorded missing evidence.
                self.snapshot_refs=[]
            self.index = latest
            self.snapshot_refs = [refreshed, *(ref for ref in self.snapshot_refs if ref != refreshed)]
        from pdf_drawing_readings import apply as apply_drawing_readings
        self.index = apply_drawing_readings(context, record, self.version, self.index)
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
        self.coverage['inline_raster'] = {
            str(page + 1): quality['inline_raster'] for page, quality in self.index.quality.items()
            if quality.get('inline_raster', {}).get('detected')}
        self.coverage['inline_raster_coverage_measured'] = self.index.metadata.get('preparation_version', 0) >= 2
        from pdf_page_routing import coverage_for_pages
        self.coverage.update(coverage_for_pages(self.index))
        self.coverage['drawing_readings'] = self.index.metadata.get('drawing_readings', {})
        from normative_multidoc import attach
        attach(self,payload)
        from normative_engineering_tools import attach as attach_engineering
        attach_engineering(self, context, record)

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
        from normative_source_registry import merge
        merge(self.sources,[source])
        return {'source_id': source['id']}

    def quantity_candidates(self, query, *, unit='', height_cm=None, section_context='') -> list[dict[str, Any]]:
        """Precompute bounded alternatives; never use the declared VOR amount."""
        if self.index is None or not self.version:
            return []
        from normative_table_candidates import grouped_totals
        from normative_retrieval import terms
        from normative_scope_tokens import scope_tokens,compare_scopes,physical_row_labels
        scope_terms = terms(section_context)-terms(query)
        requested_scope_ids=scope_tokens(section_context)
        ranked = []
        # Coverage is a bounded MODEL preview, not an internal search index.
        # Drawing frames can fill that preview before quantity schedules at
        # the end of a large PDF. Search every prepared table exactly once.
        for page, tables in enumerate(self.index.tables or []):
            tables = self._verified_tables(page+1,tables)
            for table in tables:
                captions = [c['text'] for c in table['cells'] if c.get('role')=='header'
                    and c.get('colspan',1)>=table['col_count'] and c.get('text','').strip()]
                row_labels=physical_row_labels(table)
                # Generic labels alone ("улица", "участок") do not identify
                # a named location. Distinctive words and typed IDs may add
                # discovery evidence from the table's own physical rows.
                row_wanted=scope_terms-{'улиц','участо','секци','путь','объект','проект','дорог','работ'}
                matching_labels=[label for label in row_labels if row_wanted & terms(label['text'])]
                row_terms=set().union(*(terms(label['text'])for label in matching_labels))
                # Scope locates a construction; it cannot substitute for a
                # work/material match in a wholly unrelated quantity table.
                for candidate in grouped_totals(table, query, unit=unit, height_cm=height_cm,
                        include_single=True,include_parameter_alternatives=True):
                    scoped_text = ' '.join([*candidate['headers'],*captions])
                    found_terms = terms(scoped_text)
                    abbreviations = re.findall(r'\b([а-я]{3,5})\.',scoped_text.casefold())
                    matched = {term for term in scope_terms if term in found_terms
                        or (not term.startswith('scope:') and any(not found.startswith('scope:') and
                            min(len(term),len(found))>=4 and (term.startswith(found) or found.startswith(term)) for found in found_terms))
                        or (not term.startswith('scope:') and any(term.startswith(abbreviation) for abbreviation in abbreviations))}
                    matched.update(row_wanted & row_terms)
                    observed_scope_ids=scope_tokens(scoped_text+'\n'+'\n'.join(label['text']for label in row_labels))
                    # Section context is a bounded extra signal. A common
                    # street caption must not outrank an exact material and
                    # thickness match merely because it has one more word.
                    ranking_score=candidate['score']+min(8,2*len(matched))+2*bool(candidate['unit_verified'])
                    ranked.append(dict(**candidate, page=page+1, table_id=table['table_id'],
                        section_match_terms=sorted(matched),context_headers=captions,ranking_score=ranking_score,
                        requested_scope_identifiers=sorted(requested_scope_ids),observed_scope_identifiers=sorted(observed_scope_ids),
                        scope_identifier_match_state=compare_scopes(requested_scope_ids,observed_scope_ids),
                        scope_match_observations=[dict(label,read_request=dict(tool='read_table',page=page+1,
                            document_version_id=self.version,table_id=table['table_id'],row_ids=[label['row']],column_ids=[0]))
                            for label in matching_labels[:3]],scope_match_observation_count=len(matching_labels),
                        scope_observations_prove_total_allocation=False))
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

    def execute(self, request) -> dict[str, Any]:
        if request.tool == 'read_source' and request.source_id.startswith('tool-result:'):
            return self._read_tool_result(request)
        from normative_engineering_tools import execute as engineering_execute
        related = engineering_execute(self, request)
        if related is not None:
            return self._archive_tool_result(related)
        if request.tool == 'read_page' and request.bbox:
            from normative_drawing_tool import render_region
            return render_region(self, request)
        from normative_multidoc import route
        routed=route(self,request)
        result = routed if routed is not None else self._execute_single(request)
        if request.tool == 'search_project' and getattr(self, 'engineering_graph', None):
            from normative_engineering_relations import search
            result = {**result, 'engineering_entities': search(self.engineering_graph,
                request.query, offset=request.entity_offset)}
        return self._archive_tool_result(result)

    def _archive_tool_result(self, result):
        """Navigation archive is not a document source or a citation handle."""
        if len(json.dumps(result, ensure_ascii=False)) <= 3500:
            return result
        from analysis_replay import digest
        handle = 'tool-result:'+digest(result)[:32]
        archive = getattr(self, 'result_archive', {})
        self.result_archive = archive
        if len(archive) < 256 or handle in archive:
            archive[handle] = deepcopy(result)
            return dict(result, result_handle=handle,
                archive_read_request=dict(tool='read_source', source_id=handle, offset=0))
        return dict(result, archive_error='tool_result_archive_budget_exhausted')

    def _read_tool_result(self, request):
        archive = getattr(self, 'result_archive', {})
        value = archive.get(request.source_id)
        if value is None:
            # Saved tool receipts survive follow-up. Rebuild only a hash-matched
            # archive, never accept arbitrary tool-result handles as evidence.
            from analysis_replay import digest
            for event in self.events:
                candidate = event.get('result') or {}
                if candidate.get('result_handle') == request.source_id:
                    original = {k: v for k, v in candidate.items() if k not in {'result_handle', 'archive_read_request'}}
                    if request.source_id == 'tool-result:'+digest(original)[:32]:
                        value = original
                        break
        if value is None:
            return {'error': 'unknown_tool_result'}
        text = json.dumps(value, ensure_ascii=False)
        if request.offset > len(text):
            return {'error': 'source_offset_out_of_range', 'full_length': len(text)}
        piece = text[request.offset:request.offset+3000]
        def reply(part):
            consumed = request.offset+len(part)
            return dict(result_handle=request.source_id, text=part,
                offset=request.offset, full_length=len(text),
                next_offset=consumed if consumed < len(text) else None,
                navigation_only=True, document_evidence=False)
        # JSON escaping also consumes the response budget. The cursor follows
        # delivered characters, never a nominal chunk that could be truncated.
        while len(json.dumps(reply(piece), ensure_ascii=False)) > 3500:
            piece = piece[:max(1, len(piece)*4//5)]
        return reply(piece)

    def _execute_single(self, request):
        if request.tool == 'read_source':
            source = next((s for s in self.sources if s['id'] == request.source_id), None)
            if source is None:
                return {'error': 'unknown_source'}
            text = source['text']
            if request.offset > len(text):
                return {'error': 'source_offset_out_of_range', 'full_length': len(text)}
            self.preferred.append(source['id'])
            if request.offset:
                source = {**source, 'id':source['id']+f':part:{request.offset}', 'text':text[request.offset:request.offset+3500]}
                self.preferred.append(source['id'])
                return {**self._add(source), 'text': source['text'], 'full_length': len(text),
                    'offset': request.offset,
                    'next_offset': request.offset+3500 if request.offset+3500 < len(text) else None}
            return {'source_id': source['id'], 'full_length':len(text), 'offset': 0,
                'next_offset': 3500 if len(text)>3500 else None}
        if request.tool == 'sum_cells':
            return self._sum(request)
        if request.tool == 'search_norms':
            if self.kb is None:
                return {'error': 'normative_kb_unavailable'}
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
                # A deliberately requested new source must survive packing.
                # Otherwise a late raster label can disappear again behind the
                # generic 1300-character normative preview after being found.
                self.preferred.append(source['id'])
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
            from normative_prepared_retrieval import PreparedTableSearch, combine
            if getattr(self,'prepared_table_search',None) is None:
                self.prepared_table_search=PreparedTableSearch(self.index)
            search = combine(self.project_search,self.prepared_table_search,request.query,
                tables=request.tool == 'search_tables',offset=request.offset)
            pages = []
            for hit in search['hits']:
                pages.append(dict(**self._page(hit['page'], hit['offset']), preview=hit['preview'],
                    **({'matched_table_observations':hit['table_observations'],
                        'matched_table_observation_count':hit['table_observation_count'],
                        'matched_table_observations_truncated':hit['table_observations_truncated']}
                        if hit.get('table_observations') else {})))
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
        header_parameters=[]
        from pdf_saved_evidence import header_geometry
        for header in cells:
            if header.get('role')!='header':
                continue
            facts=header_geometry(header)
            if not facts:
                continue
            ancestors=[h['text'] for h in cells if h['cell_id'] in header.get('header_ids',[])]
            text=' | '.join([*ancestors,header['text']])
            if len(text)>900:
                continue
            source_id=cell_source_id(self.version,request.page,table['table_id'],header['cell_id']).replace(':cell:',':header:')
            from pdf_source_links import source_link
            link=source_link(self.index,self.version,request.page,table,header)
            if link['state'] in {'invalid_geometry','source_geometry_requires_review'}:
                continue
            self._add(dict(id=source_id,kind='project',version_id=self.version,page=request.page,
                label=self.filename,text=text,origin='ocr_verified_header',numeric_verified=True,
                verified_parameters=facts,rects=link['rects'],source_anchor=link))
            header_parameters.extend(dict(**{k:v for k,v in f.items() if k!='version'},source_id=source_id,
                scope_quote=text,scope_state='unresolved') for f in facts)
            self.preferred.append(source_id)
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
            from normative_table_headers import table_scope_context
            scope_context=table_scope_context(table,cell,cell_id,self.version,request.page)
            self.cells[cell_id] = dict(cell=cell, headers=text, header_text=header_text, page=request.page,unit=resolved_unit,
                version_id=self.version,table_id=table['table_id'])
            from normative_header_observations import addresses
            from pdf_source_links import source_link
            link=source_link(self.index,self.version,request.page,table,cell)
            invalid_geometry=link['state'] in {'invalid_geometry','source_geometry_requires_review'}
            output.append(self._add(dict(id=cell_id, kind='project',
                **({'table_scope_context':scope_context} if scope_context else {}),
                version_id=self.version, page=request.page, label=self.filename,
                text=text, literal_text=cell['text'], source_anchor=link, rects=link['rects'],
                header_observation_addresses=addresses(table,[cell]),table_id=table['table_id'],
                ocr_observations=[h['header_line_observation'] for h in cells
                    if h.get('header_line_observation') and h['cell_id'] in [cell['cell_id'],*cell.get('header_ids',[])]],
                numeric_verified=bool(cell.get('numeric_verified')) and not invalid_geometry,
                verified_parameters=[f for h in cells if h['cell_id'] in cell.get('header_ids',[]) for f in header_geometry(h)],
                unit_verified=bool(resolved_unit) and not invalid_geometry,unit=resolved_unit,
                origin=(cell.get('origin', table.get('origin', 'uncertain_table'))
                    if cell.get('quality', 'usable') == 'usable' and not invalid_geometry else 'uncertain_table'))))
            if request.row_ids and request.column_ids:
                self.preferred.append(cell_id)
        from normative_retrieval import terms
        wanted = terms(request.query)
        matching_rows = sorted({c['row'] for c in cells if wanted & terms(c['text'])})[:20]
        from normative_header_observations import project
        observations=project(table,selected,page=request.page,offset=request.header_line_offset)
        from pdf_row_measurements import observations as paired_row_observations
        from pdf_source_links import source_link
        row_measurements=paired_row_observations(table,selected)
        for measurement in row_measurements:
            measurement.update(page=request.page,table_id=table['table_id'],
                value_source_id=cell_source_id(self.version,request.page,table['table_id'],measurement['value_cell_id']),
                read_request=dict(tool='read_table',page=request.page,document_version_id=self.version,
                    table_id=table['table_id'],row_ids=[measurement['row']],
                    column_ids=[measurement['unit_column'],measurement['value_column']]))
            measurement['source_anchors']=[source_link(self.index,self.version,request.page,table,
                next(c for c in cells if c['cell_id']==measurement[key]))for key in ('unit_cell_id','value_cell_id')]
            from normative_paired_quantities import source_fields
            from normative_paired_scope import observe,preview
            paired_source=source_fields(table,measurement,version=self.version,
                page=request.page,label=self.filename,
                parent_context=observe(self.index,self.version,request.page,table,measurement))
            measurement['source_id']=self._add(paired_source).get('source_id')
            parent=preview(paired_source)
            if parent:
                measurement['parent_observation']=parent
        from normative_scalar_rows import observations as scalar_rows
        for row in scalar_rows(self.index, self.version, request.page, table, selected, self.filename):
            registered = self._add(row['source'])
            if 'source_id' in registered:
                row_measurements.append(dict(row['preview'], source_id=registered['source_id'],
                    measurement_kind='native_scalar_row'))
        return {'row_measurements':row_measurements,'header_observations':observations,'cells': output, 'header_parameters':header_parameters, 'matching_rows': matching_rows, 'columns': table_outline(table)['columns'], 'rows': [dict(**{k: c[k] for k in ('cell_id', 'row', 'column', 'text', 'header_ids', 'bbox', 'quality', 'numeric_verified','row_scope') if k in c},
            **output[i]) for i,c in enumerate(selected)], 'next_offset': request.offset+4, 'row_count': table['row_count']}

    def _sum(self, request):
        documents={s.get('version_id') for s in self.sources if s['id'] in request.operand_ids}
        if len(documents)>1 or documents and documents!={self.version}:
            return {'error':'cross_document_sum_requires_separate_scope_validation'}
        import hashlib
        from normative_parameters import number, measure
        ids = request.operand_ids
        if not 2 <= len(ids) <= 12 or len(set(ids)) != len(ids) or not request.unit:
            return {'error': 'distinct_numeric_operands_and_unit_required'}
        # A continuation restores source envelopes, while the transient cell
        # lookup starts empty. Reopen only the exact already cited cells from
        # the pinned prepared tables; this does not perform OCR or choose any
        # operands. _add checks that the restored envelope has not changed.
        for sid in ids:
            if sid in self.cells:
                continue
            source=next((s for s in self.sources if s['id']==sid),None)
            if (not source or getattr(self,'index',None) is None or source.get('kind')!='project'
                    or source.get('version_id')!=self.version
                    or type(source.get('page')) is not int
                    or not 1<=source['page']<=len(self.index.texts)):
                continue
            for table in self._tables(source['page']):
                cell=next((c for c in table['cells']
                    if sid in {cell_source_id(self.version,source['page'],table['table_id'],c['cell_id']),
                        cell_source_id(self.version,source['page'],table['table_id'],c['cell_id'],envelope_version=0)}),None)
                if cell is not None:
                    reopened=self._execute_single(EvidenceRequest(tool='read_table',page=source['page'],
                        table_id=table['table_id'],row_ids=[cell['row']],column_ids=[cell['column']]))
                    current=cell_source_id(self.version,source['page'],table['table_id'],cell['cell_id'])
                    if sid!=current:
                        return {'error':'source_handle_version_changed',
                            'replacement_source_ids':{sid:current},'read_result':reopened}
                    break
        cells = [self.cells.get(i) for i in ids]
        if any(c is None for c in cells):
            return {'error': 'unknown_cell'}
        cells = [c for c in cells if c is not None]
        if any((s.get('source_anchor') or {}).get('state') in {
                'invalid_geometry','source_geometry_requires_review'} for s in self.sources if s['id'] in ids):
            return {'error': 'source_geometry_requires_review'}
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
        from pdf_inline_evidence import excerpt_origin, native_fragments
        namespace=':doc:'+str(self.version) if getattr(self,'additional_documents',None) or getattr(self,'is_supporting_document',False) else ''
        ref = self._add(dict(id=f'agent:p{page}:{offset}'+namespace, kind='project', page=page,
            version_id=self.version, label=self.filename, text=piece,
            origin=excerpt_origin(self.index, page, offset, len(piece))))
        if exact:
            self.preferred.append(ref.get('source_id', ''))
        readable = []
        if excerpt_origin(self.index, page, offset, len(piece)) == 'ocr_inline_unreviewed':
            for fragment in native_fragments(self.index, page, offset, len(piece)):
                source = self._add(dict(id=f"agent:p{page}:{fragment['offset']}:native:{len(fragment['text'])}"+namespace,
                    kind='project', page=page, version_id=self.version, label=self.filename,
                    text=fragment['text'], origin='native'))
                readable.append(dict(**source, offset=fragment['offset'], origin='native',
                    text=fragment['text']))
        tables = self._tables(page)
        return dict(**ref, page=page, document_version_id=self.version, next_offset=offset+3500 if offset+3500 < len(text) else None,
            tables=[table_outline(t) for t in tables], native_fragments=readable)

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
        if hasattr(self,'context'):
            from normative_table_headers import apply
            tables = apply(self.context,self.record,self.version,page,tables,self.events)
        from pdf_saved_evidence import replay_unit_proofs
        tables, restored = replay_unit_proofs(tables)
        for event in restored:
            event.update(page=page, version_id=self.version)
            if event not in self.events:
                self.events.append(event)
        return tables


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


def run(ask, client, credentials, instructions, inputs, schema, trace, stats, tools, *,
        verify_answer=None, evidence_timeout_seconds=120):
    """Three calls; one source-only repair for a newly exposed conditional error."""
    from normative_context import ModelOutputLimitError, ModelProviderResponseError
    started = time.monotonic()
    history = []
    feedback = []
    seen_conditions=set()
    previous_invalid_parameters=set()
    late_repair=False
    successful_rounds=0
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
        if any(s.get('paired_parent_context')for s in tools.sources):
            supplied['paired_parent_observation_rules'] = (
                'parent_observation / paired_parent_observation связывает дочерний номер с физической родительской строкой. '
                'Это наблюдение, а не подтверждение инженерной принадлежности или OCR описания. '
                'Прочитай read_request, проверь операцию, участок и обозначение материала отдельно. '
                'Одинаковое количество не отменяет различия ШТУ, прямого/кривого участка и монтажа/демонтажа. '
                'При неоднозначности сохрани scope_state=unresolved.')
        supplied['previous_literal_parameters'] = list(getattr(tools,'parameter_evidence',{}).values())
        supplied['previous_claim_citations'] = claim_memory.context()
        owner_work=(inputs.get('normative_work_owner') or {}).get('work') or inputs.get('position',{}).get('work','')
        supplied['normative_thickness_adjustments'] = thickness_adjustments(owner_work,tools.sources)
        planning=successful_rounds==0
        stage = 'normative-evidence-plan' if planning else f'normative-evidence-{step}'
        step_instructions = instructions+TOOLS
        if planning and MAX_ROUNDS > 1:
            # Navigation is a separate bounded task, not a full audit of every
            # preliminary claim before the underlying table has been read.
            from normative_agent_planning import navigation
            step_instructions = navigation(inputs, instructions, TOOLS)
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
        from normative_context_packing import completed_read_ids
        supplied = compact(supplied,tools.sources,tools.preferred,planning=planning,
            read_source_ids=completed_read_ids(tools.events),
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
            answer = restore_source_ids(ask(client, credentials, step_instructions, supplied, schema, trace, stage, stats),aliases,tools.sources)
            packing['response_state'] = 'completed'
        except ModelOutputLimitError:
            packing['response_state'] = 'output_limit'
            if step >= MAX_ROUNDS-1:
                raise
            feedback = [
                'Предыдущий ответ не поместился. Верни короткий JSON. '
                'Не повторяй каталог источников и пояснения. Для каждого вывода оставь '
                'точный claim_id, необходимые ссылки и одно короткое предложение.'
            ]
            continue
        except ValidationError as exc:
            packing['response_state'] = 'invalid_schema'
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
            packing['response_state'] = 'provider_failure'
            code = exc.code if isinstance(exc, ModelProviderResponseError) else getattr(exc, 'status_code', None)
            retryable=isinstance(exc,openai.APIConnectionError) or code in {408,429,500,502,503,504}
            if not retryable or step>=MAX_ROUNDS-1:
                raise
            stats.setdefault('warnings',[]).append('Временный сбой провайдера; повтор в оставшемся бюджете шагов, без повторного OCR.')
            feedback=['Предыдущий запрос не завершился из-за временного сбоя соединения. Прочитанные источники сохранены; выполни проверку по ним.']
            continue
        except Exception:
            packing['response_state'] = 'failed'
            raise
        successful_rounds+=1
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
        if step >= MAX_ROUNDS-1 or time.monotonic()-started > evidence_timeout_seconds:
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
