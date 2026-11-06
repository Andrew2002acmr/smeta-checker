"""Version-pinned engineering navigation over existing immutable PG artifacts.

Document hierarchy and workbook ownership are candidates, not engineering proof.
Only the fact ledger's separately validated bindings may verify a quantity edge.
This layer does not change a review or promote a model's proposed relationship.
"""

from copy import deepcopy
import json
import re

from analysis_replay import digest
from audit_models import position_source_dict

# V1 artifacts were development prototypes with coupled observations and
# transport-dependent identities. Preserve them, but never load/reinterpret
# them as this canonical independent-observation/material-navigation contract.
VERSION = 2
STAGE = 'engineering_relations_v2'
STATES = {'candidate', 'verified', 'rejected', 'stale'}


def _pins(record):
    return dict(project_id=str(record['project_id']),
                vor_version_id=str(record['vor_version_id']),
                pdf_versions=sorted(set(str(v) for v in record['config'].get('pdf_versions', {}).values())))


def _id(kind, version, address):
    return 'engineering:' + kind + ':' + digest(dict(version=str(version), address=address))[:32]


def _entity(kind, version, address, label, *, origin, **extra):
    return dict(id=_id(kind, version, address), kind=kind, version_id=str(version),
                address=address, label=str(label), origin=origin, **extra)


def _combine_evidence(left, right):
    if ({k: v for k, v in left.items() if k != 'evidence_ids'}
            == {k: v for k, v in right.items() if k != 'evidence_ids'}):
        return dict(left, evidence_ids=sorted(set(left.get('evidence_ids', [])) | set(right.get('evidence_ids', []))))
    return None


def _edge(kind, source, target, version, evidence, method, reason, state='candidate'):
    if state not in STATES:
        raise ValueError('Unknown engineering relation state')
    identity = dict(kind=kind, source=source, target=target, version_id=str(version),
                    evidence_ids=sorted(set(evidence)), method=method)
    return dict(id=_id('relation', version, identity), **identity, state=state, reason=reason)


def _canonical_inputs(sources, ledgers):
    """Normalize only after checking both transport attestation and raw hash."""
    from normative_source_identity import canonical_source_id
    source_map, mapping, raw_hashes, aliases = {}, {}, {}, {}
    for source in sources:
        canonical = canonical_source_id(source)
        if canonical is None:
            continue
        normalized = {k: deepcopy(v) for k, v in source.items()
                      if k not in {'id', 'physical_source_id', 'source_alias'}} | dict(id=canonical)
        if canonical in source_map and source_map[canonical] != normalized:
            raise ValueError('Canonical engineering source collision')
        source_map[canonical] = normalized
        mapping[source['id']], raw_hashes[source['id']] = canonical, digest(source)
        if canonical != source['id']:
            aliases[source['id']] = dict(canonical_id=canonical, original_sha256=digest(source),
                                        method='validated_content_bound_source_alias')
    normalized_ledgers = []
    for ledger in ledgers:
        value = deepcopy(ledger)
        value['source_hashes'] = {mapping[sid]: digest(source_map[mapping[sid]])
            for sid, expected in ledger.get('source_hashes', {}).items()
            if sid in mapping and raw_hashes[sid] == expected}
        for fact in value.get('facts', []):
            fact['source_id'] = mapping.get(fact.get('source_id'), fact.get('source_id'))
            fact['operand_source_ids'] = [mapping.get(sid, sid) for sid in fact.get('operand_source_ids', [])]
        normalized_ledgers.append(value)
    return list(source_map.values()), normalized_ledgers, aliases


def _observation(fact):
    return {k: deepcopy(v) for k, v in fact.items() if k not in {
        'scope_id', 'owner_position', 'proposed_owner_position', 'subject_state',
        'subject_binding_state', 'comparison_state', 'comparison_states', 'scope_quote',
        'binding_issues', 'quantity_role', 'quote',
    }}


def _operand_dependencies(fact, hashes, sources):
    if (fact.get('operation') != 'sum' or fact.get('literal_state') != 'verified'
            or fact.get('calculation_state') != 'verified'):
        return []
    inputs = [fact['source_id'], *fact.get('operand_source_ids', [])]
    if any(sid not in sources or hashes.get(sid) != digest(sources[sid]) for sid in inputs):
        return []
    from normative_parent_basis import _proposal_dependencies
    try:
        return _proposal_dependencies(fact, sources)[1:]
    except (KeyError, TypeError, ValueError):
        return []


def _material_description(source, parent):
    """Address one literal child description; classification stays a candidate."""
    pair=source.get('paired_measurement_context')or{}
    cells=pair.get('row_cells')or[]
    unit=next((c for c in cells if c.get('cell_id')==pair.get('unit_cell_id')),None)
    child=parent['child']['cell']
    if unit is None:
        return None
    descriptions=[c for c in cells if c.get('row')==pair.get('row')
        and child['column']<c.get('column',-1)<unit['column'] and c.get('text','').strip()]
    if len(descriptions)!=1:
        return None
    description=descriptions[0]
    text=description['text'].strip()
    action=r'^\s*(?:устройств|уклад|монтаж|демонт|разбор|установ|замен|сняти|перевоз|транспорт|очист|ремонт|погруз|выгруз)'
    material=r'\b(?:шпал|рельс|скреплен|болт|смес|щеб|плит|камень|камн|геотекст|балласт|брус|шайб|гайк|подклад|наклад|проклад)\w*'
    if re.search(action,text,re.I)or not re.search(material,text,re.I):
        return None
    return description


def build(record, positions, *, ledgers=(), sources=(), scope_graph=None):
    """Index workbook navigation plus already validated project facts.

    Caller supplies ledger output, not raw model proposals. Source hashes are
    rechecked here; graph construction never infers ownership from a number.
    """
    pins = _pins(record)
    ledgers = list(ledgers)
    sources, ledgers, aliases = _canonical_inputs(sources, ledgers)
    entities, relations = {}, {}

    def add(entity):
        existing = entities.get(entity['id'])
        if existing is not None and existing != entity:
            combined = _combine_evidence(existing, entity)
            if combined is None:
                raise ValueError('Engineering entity address collision')
            entity = combined
        entities[entity['id']] = entity
        return entity['id']

    def link(*args, **kwargs):
        edge = _edge(*args, **kwargs)
        relations[edge['id']] = edge

    def add_evidence(source):
        sid, version = source['id'], str(source['version_id'])
        return add(_entity('Evidence', version,
            dict(source_id=sid, source_sha256=digest(source)), source.get('label', sid),
            origin='project', source_id=sid, page=source.get('page'),
            read_request=dict(tool='read_source', source_id=sid)))

    # This reuses the actual workbook graph rather than replacing its parser.
    if scope_graph is None:
        from normative_scope_graph import build as workbook_graph
        scope_graph = workbook_graph(positions)
    work_ids = {}
    for index, position in sorted(positions.items()):
        source = position_source_dict(position) or {}
        address = dict(position_index=index, source=source)
        kind = 'Material' if position.position_type.strip().casefold() in {'material', 'материал'} else 'Work'
        work_ids[index] = add(_entity(kind, pins['vor_version_id'], address,
            position.work_name, origin='workbook', position_index=index,
            declared_quantity=position.volume, unit=position.unit))
        node = scope_graph.get('nodes', {}).get(str(index), {})
        if node.get('scope_id'):
            construction = add(_entity('Construction', pins['vor_version_id'],
                dict(workbook_scope_id=node['scope_id']),
                ' / '.join(s.get('text', '') for s in node.get('sections', [])) or 'Раздел ВОР',
                origin='workbook', scope_id=node['scope_id']))
            link('PART_OF', work_ids[index], construction, pins['vor_version_id'], [],
                 'workbook_section', 'Структура ВОР; проектная принадлежность не установлена.')
    for edge in scope_graph.get('edges', []):
        if edge['source'] in work_ids and edge['target'] in work_ids:
            material = entities[work_ids[edge['target']]]['kind'] == 'Material'
            link('USES_MATERIAL' if material else 'PART_OF',
                 work_ids[edge['source']] if material else work_ids[edge['target']],
                 work_ids[edge['target']] if material else work_ids[edge['source']], pins['vor_version_id'], [],
                 'workbook:' + edge['kind'], 'Навигация по ВОР; не является доказательством ПД.')

    source_map = {s['id']: s for s in sources if s.get('id')}
    retained_sources = {}
    for ledger in ledgers:
        for sid, expected in ledger.get('source_hashes', {}).items():
            candidate = source_map.get(sid)
            if (candidate and candidate.get('kind') == 'project'
                    and str(candidate.get('version_id')) in pins['pdf_versions'] and digest(candidate) == expected):
                retained_sources[sid] = deepcopy(candidate)
        for fact in ledger.get('facts', []):
            sid = fact.get('source_id')
            source = source_map.get(sid)
            if (not source or source.get('kind') != 'project'
                    or str(source.get('version_id')) not in pins['pdf_versions']
                    or ledger.get('source_hashes', {}).get(sid) != digest(source)):
                continue
            version = str(source['version_id'])
            evidence_id = add_evidence(source)
            # A project observation is independent of the inspected workbook
            # quantity, proposed owner and the result of comparing them.
            observation = _observation(fact)
            fact_id = add(_entity('Fact', version, dict(fact_id=fact['id']),
                str(fact.get('value', '')) + ' ' + str(fact.get('unit', '')),
                origin='project', fact=observation))
            literal = fact.get('literal_state') == 'verified'
            link('SUPPORTED_BY', fact_id, evidence_id, version, [evidence_id], 'validated_fact_ledger',
                 'Буквальное значение проверено по первоисточнику.' if literal else 'Буквальное значение требует проверки.',
                 'verified' if literal else 'candidate')
            for operand in _operand_dependencies(fact, ledger.get('source_hashes', {}), source_map):
                operand_id = add_evidence(source_map[operand])
                link('DERIVED_FROM', fact_id, operand_id, version, [evidence_id, operand_id],
                     'validated_fact_ledger', 'Исходная ячейка входит в проверенную сумму.', 'verified')
            project_work = add(_entity('Work', version,
                dict(source_id=sid, scope_quote=fact.get('scope_quote', '')),
                fact.get('scope_quote') or source.get('text', '')[:300] or source.get('label', sid),
                origin='project', evidence_ids=[evidence_id],
                read_request=dict(tool='read_source', source_id=sid)))
            bound = (literal and fact.get('subject_binding_state', fact.get('subject_state')) == 'verified'
                     and fact.get('owner_position') in work_ids)
            link('HAS_QUANTITY', project_work, fact_id, version, [evidence_id], 'validated_fact_ledger',
                 'Принадлежность проверена отдельно от сравнения ВОР.' if bound else 'Принадлежность величины не доказана.',
                 'verified' if bound else 'candidate')
            owner = fact.get('owner_position') if bound else fact.get('proposed_owner_position')
            if owner in work_ids:
                link('CORRESPONDS_TO', work_ids[owner], project_work, version, [evidence_id],
                     'validated_fact_ledger', 'Проверенная принадлежность ПД.' if bound else 'Предлагаемая принадлежность; нужна проверка.',
                     'verified' if bound else 'candidate')
            # Numbered parents are physically addressed observations. Even an
            # intact parent row does not prove the work applies to this site.
            from normative_paired_scope import validated_parent
            parent = validated_parent(source) if source.get('paired_parent_context') else None
            if parent:
                binding = parent['binding']
                parent_row = parent['parent']['cell']['row']
                construction = add(_entity('Construction', version,
                    dict(page=binding['page'], table_id=binding['table_id'], parent_row=parent_row),
                    parent['description']['cell']['text'], origin='project', evidence_ids=[evidence_id],
                    read_request=dict(tool='read_table', document_version_id=version,
                        page=binding['page'], table_id=binding['table_id'], row_ids=[parent_row])))
                link('PART_OF', project_work, construction, version, [evidence_id],
                     'document_number_hierarchy', 'Нумерованный родитель прочитан; инженерная применимость не доказана.')
                description=_material_description(source,parent)
                if description is not None:
                    pair=source['paired_measurement_context']
                    columns=sorted({description['column'],*[c['column']for c in pair['row_cells']
                        if c['cell_id']in{pair['unit_cell_id'],pair['value_cell_id']}]})
                    material=add(_entity('Material',version,dict(page=binding['page'],table_id=binding['table_id'],
                        row=binding['row'],cell_id=description['cell_id']),description['text'],origin='project',
                        classification_state='candidate',evidence_ids=[evidence_id],
                        read_request=dict(tool='read_table',document_version_id=version,page=binding['page'],
                            table_id=binding['table_id'],row_ids=[binding['row']],column_ids=columns)))
                    link('USES_MATERIAL',construction,material,version,[evidence_id],'literal_material_candidate',
                         'В дочерней строке назван материал; классификация и инженерное применение ещё не подтверждены.')
                    link('DESCRIBES',fact_id,material,version,[evidence_id],'literal_row_candidate',
                         'Величина прочитана в этой строке; принадлежность материалу требует отдельной проверки.')
            # Literal locations remain address-specific candidates; equal
            # names on different pages are never automatically merged.
            for match in re.finditer(r'\bПК\s*\d+(?:\s*\+\s*\d+(?:[.,]\d+)?)?(?:\s*[-–]\s*(?:ПК\s*)?\d+(?:\s*\+\s*\d+(?:[.,]\d+)?)?)?',
                                     fact.get('scope_quote', ''), re.I):
                area_id = add(_entity('ProjectArea', version,
                    dict(source_id=sid, literal=match.group()), match.group(), origin='project', evidence_ids=[evidence_id]))
                link('PART_OF', project_work, area_id, version, [evidence_id], 'literal_location_candidate',
                     'Пикет найден в контексте; границы применения требуют доказательства.')
    return dict(schema=VERSION, **pins, entities=[entities[k] for k in sorted(entities)],
                relations=[relations[k] for k in sorted(relations)],
                sources=[retained_sources[k] for k in sorted(retained_sources)],
                ledger_proofs=deepcopy(list(ledgers)),
                source_aliases=aliases,
                policy='Candidate relations never authorize engineering conclusions; document versions are exact pins.')


def _supports(fact, operands, relation, left, right, evidence_sids):
    literal = fact.get('literal_state') == 'verified' and fact.get('source_id') in evidence_sids
    bound = fact.get('subject_binding_state', fact.get('subject_state')) == 'verified'
    if not literal:
        return False
    if relation['kind'] == 'SUPPORTED_BY':
        return (left['kind'] == 'Fact' and left['address'].get('fact_id') == fact['id']
                and left.get('fact') == _observation(fact) and right['kind'] == 'Evidence'
                and right['id'] in relation['evidence_ids'] and right.get('source_id') == fact.get('source_id'))
    if relation['kind'] == 'DERIVED_FROM':
        sid = right.get('source_id')
        return (left['kind'] == 'Fact' and left['address'].get('fact_id') == fact['id']
                and left.get('fact') == _observation(fact) and right['kind'] == 'Evidence'
                and right['id'] in relation['evidence_ids'] and sid in operands
                and evidence_sids == {fact['source_id'], sid})
    if relation['kind'] == 'HAS_QUANTITY':
        return (bound and left['kind'] == 'Work' and left['origin'] == 'project'
                and left['address'].get('source_id') == fact.get('source_id')
                and left['address'].get('scope_quote') == fact.get('scope_quote', '')
                and right['kind'] == 'Fact' and right['address'].get('fact_id') == fact['id']
                and right.get('fact') == _observation(fact))
    if relation['kind'] == 'CORRESPONDS_TO':
        return (bound and left['origin'] == 'workbook' and left.get('position_index') == fact.get('owner_position')
                and right['origin'] == 'project' and right['kind'] == 'Work'
                and right['address'].get('source_id') == fact.get('source_id')
                and right['address'].get('scope_quote') == fact.get('scope_quote', ''))
    return False


def _validate(graph, record):
    if graph.get('schema') != VERSION or any(graph.get(k) != v for k, v in _pins(record).items()):
        raise ValueError('Engineering graph document pins differ')
    entities = {e['id']: e for e in graph['entities']}
    if len(entities) != len(graph['entities']):
        raise ValueError('Duplicate engineering entity identity')
    allowed = {*_pins(record)['pdf_versions'], str(record['vor_version_id'])}
    for e in entities.values():
        if e.get('version_id') not in allowed or e['id'] != _id(e['kind'], e['version_id'], e['address']):
            raise ValueError('Engineering entity identity/version mismatch')
        if e['kind']=='Material'and e.get('origin')=='project'and e.get('classification_state')!='candidate':
            raise ValueError('Literal material discovery cannot verify classification')
    source_map = {s['id']: s for s in graph.get('sources', [])}
    for entity in entities.values():
        if entity['kind'] == 'Evidence':
            source = source_map.get(entity.get('source_id'))
            if (source is None or entity['address'].get('source_id') != source['id']
                    or entity['address'].get('source_sha256') != digest(source)
                    or str(source.get('version_id')) != entity['version_id']):
                raise ValueError('Engineering evidence envelope does not match its source')
    proofs = []
    for ledger in graph.get('ledger_proofs', []):
        for fact in ledger.get('facts', []):
            source = source_map.get(fact.get('source_id'))
            if (source and ledger.get('source_hashes', {}).get(source['id']) == digest(source)
                    and str(source.get('version_id')) in _pins(record)['pdf_versions']):
                proofs.append((fact, _operand_dependencies(fact, ledger.get('source_hashes', {}), source_map)))
    for relation in graph['relations']:
        if relation['source'] not in entities or relation['target'] not in entities or relation['state'] not in STATES:
            raise ValueError('Engineering relation endpoints/state mismatch')
        if relation.get('version_id') not in allowed or any(e not in entities or entities[e]['kind'] != 'Evidence' for e in relation['evidence_ids']):
            raise ValueError('Engineering relation evidence/version mismatch')
        if relation['state'] == 'verified' and not relation['evidence_ids']:
            raise ValueError('Verified relation requires documentary evidence')
        expected = _edge(relation['kind'], relation['source'], relation['target'], relation['version_id'],
                         relation['evidence_ids'], relation['method'], relation['reason'], relation['state'])
        if relation['id'] != expected['id'] or not relation['reason']:
            raise ValueError('Engineering relation identity/reason mismatch')
        if any(entities[e]['version_id'] != relation['version_id'] for e in relation['evidence_ids']):
            raise ValueError('Engineering relation evidence version differs')
        if relation['state'] == 'verified' and relation['method'] != 'validated_fact_ledger':
            raise ValueError('Unvalidated method cannot verify engineering relations')
        if relation['state'] == 'verified':
            left, right = entities[relation['source']], entities[relation['target']]
            evidence_sids = {entities[e].get('source_id') for e in relation['evidence_ids']}
            if not any(_supports(fact, operands, relation, left, right, evidence_sids)
                       for fact, operands in proofs):
                raise ValueError('Verified engineering relation lacks matching validated ledger proof')
    for source in graph.get('sources', []):
        if source.get('kind') != 'project' or str(source.get('version_id')) not in _pins(record)['pdf_versions']:
            raise ValueError('Engineering source version differs')
    return graph


def store(context, record, graph):
    graph = _validate(graph, record)
    identity = dict(schema=VERSION, stage=STAGE, **_pins(record), graph_sha256=digest(graph))
    if getattr(context.repository, 'supports_analysis_replay', False) is True:
        context.repository.save_analysis_artifact(record['project_id'], identity, graph)
    return dict(key=digest(identity), sha256=digest(graph), entities=len(graph['entities']), relations=len(graph['relations']))


def load(context, record, artifact_key):
    saved = context.repository.analysis_artifact(record['project_id'], artifact_key)
    if saved is None:
        raise KeyError('Engineering graph artifact not found')
    identity, graph = saved['identity'], saved['payload']
    if (identity.get('stage') != STAGE or digest(identity) != artifact_key
            or identity.get('graph_sha256') != digest(graph)):
        raise ValueError('Engineering graph artifact integrity mismatch')
    return _validate(deepcopy(graph), record)


def load_for_run(context, record):
    """Merge only exact-pin immutable artifacts; disagreeing states stay open."""
    lookup = getattr(context.repository, 'engineering_relation_artifacts', None)
    saved = lookup(record['project_id'], record['vor_version_id'], _pins(record)['pdf_versions']) if callable(lookup) else []
    merged = dict(schema=VERSION, **_pins(record), entities=[], relations=[])
    entities, relations, sources = {}, {}, {}
    conflicts = []
    source_conflicts = set()
    ledger_proofs, aliases = {}, {}
    for item in sorted(saved, key=lambda artifact: artifact['identity']['graph_sha256']):
        graph = _validate(item['payload'], record)
        if (item['identity'].get('stage') != STAGE or any(item['identity'].get(k) != v for k, v in _pins(record).items())
                or item['identity'].get('graph_sha256') != digest(graph)):
            raise ValueError('Engineering graph artifact integrity mismatch')
        for source in graph.get('sources', []):
            sid = source['id']
            if sid in sources and sources[sid] != source:
                source_conflicts.add(sid)
            else:
                sources[sid] = deepcopy(source)
        for proof in graph.get('ledger_proofs', []):
            ledger_proofs[digest(proof)] = deepcopy(proof)
        aliases.update(graph.get('source_aliases', {}))
        for category, bank in (('entities', entities), ('relations', relations)):
            for value in graph[category]:
                existing = bank.get(value['id'])
                if existing is not None and existing != value:
                    combined = _combine_evidence(existing, value) if category == 'entities' else None
                    if combined is not None:
                        bank[value['id']] = combined
                        continue
                    # Preserve both immutable versions and never select the
                    # most convenient confirmation by ordering the results.
                    variants = existing.get('conflicting_variants', [deepcopy(existing)])
                    if value not in variants:
                        variants.append(deepcopy(value))
                    variants = sorted(variants, key=digest)
                    bank[value['id']] = dict(variants[0], conflicting_variants=variants, conflict=True)
                    if category == 'relations':
                        bank[value['id']]['state'] = 'candidate'
                        bank[value['id']]['reason'] = 'Противоречащие сохранённые основания; требуется перепроверка.'
                    conflicts.append(value['id'])
                else:
                    bank[value['id']] = deepcopy(value)
    # A reusable arithmetic fact is only as stable as its actual operands.
    invalidated_sums = {fact.get('source_id') for proof in ledger_proofs.values()
                        for fact in proof.get('facts', []) if fact.get('operation') == 'sum'
                        and source_conflicts.intersection(fact.get('operand_source_ids', []))}
    invalidated_sources = source_conflicts | invalidated_sums
    affected = set(conflicts)
    for entity in entities.values():
        source_id = (entity.get('source_id') or (entity.get('read_request') or {}).get('source_id')
                     or (entity.get('fact') or {}).get('source_id'))
        if source_id in invalidated_sources:
            entity['source_conflict'] = True
            entity.pop('read_request', None)
            affected.add(entity['id'])
    for relation in relations.values():
        if {relation['source'], relation['target'], *relation['evidence_ids']} & affected:
            relation.update(state='candidate', reason='Противоречащие сохранённые основания; требуется перепроверка.')
    merged.update(entities=[entities[k] for k in sorted(entities)], relations=[relations[k] for k in sorted(relations)],
                  sources=[sources[k] for k in sorted(sources) if k not in invalidated_sources],
                  ledger_proofs=[ledger_proofs[k] for k in sorted(ledger_proofs)],
                  source_aliases=aliases,
                  conflicts=sorted(set(conflicts)), source_conflicts=sorted(source_conflicts),
                  invalidated_sum_sources=sorted(invalidated_sums))
    return merged


def _summary(entity):
    result = {k: deepcopy(entity[k]) for k in ('id', 'kind', 'version_id', 'origin', 'source_id', 'page', 'position_index', 'read_request', 'classification_state', 'conflict', 'source_conflict') if k in entity}
    result['label'] = entity['label'][:240]
    result['label_truncated'] = len(entity['label']) > 240
    if entity['kind'] == 'Fact':
        result['fact'] = {k: entity['fact'].get(k) for k in ('value', 'unit', 'parameter', 'literal_state', 'subject_binding_state', 'subject_state', 'calculation_state', 'comparison_state')}
    return result


def _page(items, offset, limit, *, max_chars=3500, **metadata):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Invalid engineering graph pagination')
    result = dict(**metadata, offset=offset, total=len(items), items=[], next_offset=None)
    for item in items[offset:offset + limit]:
        candidate = dict(result, items=[*result['items'], item])
        if len(json.dumps(candidate, ensure_ascii=False)) > max_chars:
            break
        result['items'].append(item)
    consumed = offset + len(result['items'])
    result['next_offset'] = consumed if consumed < len(items) else None
    if not result['items'] and offset < len(items):
        raise ValueError('Engineering response item exceeds bounded envelope')
    return result


def search(graph, query, offset=0, limit=6):
    """Bounded lexical discovery, with honest candidate/verified distinctions."""
    tokens = re.findall(r'[\w.]+', query.casefold())
    scored = []
    for entity in graph['entities']:
        haystack = (entity['label'] + ' ' + entity['kind'] + ' ' + entity['id']).casefold()
        score = sum(t in haystack for t in tokens)
        if not tokens or score:
            scored.append((score, entity['id'], entity))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return _page([_summary(e) for _, _, e in scored], offset, limit, query=query,
                 discovery_only=True, policy='Entity discovery does not verify engineering relations.')


def related(graph, entity_id, offset=0, limit=6, *, entity_kind=''):
    """Bounded three-hop navigation, including unresolved path relationships."""
    entities = {e['id']: e for e in graph['entities']}
    if entity_kind not in {'','ProjectArea','Construction','Work','Material','Fact','Evidence'}:
        raise ValueError('Unknown engineering entity kind')
    if entity_id not in entities:
        raise KeyError('Unknown engineering entity')
    relations = graph['relations']
    relation_map = {edge['id']: edge for edge in relations}

    def route(rank, edge):
        uncertainty, hops, path = rank
        return uncertainty + (edge['state'] != 'verified'), hops + 1, (*path, edge['id'])

    # Find the best prefix of at most two hops for each endpoint. The final
    # edge makes a three-hop route. Prefer proof over a shorter candidate path
    # and use stable relation IDs to break ties independent of retrieval order.
    paths = {entity_id: (0, 0, ())}
    for _ in range(2):
        extended = dict(paths)
        for edge in relations:
            for start, end in ((edge['source'], edge['target']), (edge['target'], edge['source'])):
                prefix = paths.get(start)
                if prefix is None or edge['id'] in prefix[2]:
                    continue
                rank = route(prefix, edge)
                if end not in extended or rank < extended[end]:
                    extended[end] = rank
        paths = extended

    rows = []
    for edge in relations:
        candidates = [
            (route(paths[start], edge), end)
            for start, end in ((edge['source'], edge['target']), (edge['target'], edge['source']))
            if start in paths and edge['id'] not in paths[start][2]
        ]
        if not candidates:
            continue
        (_, _, path), end = min(candidates)
        unverified = [rid for rid in path if relation_map[rid]['state'] != 'verified']
        rows.append(dict(relation=deepcopy(edge), entity=_summary(entities[end]),
                         via_relation_ids=list(path), path_state='unresolved' if unverified else 'verified',
                         unverified_relation_ids=unverified,
                         evidence=[_summary(entities[e]) for e in edge['evidence_ids']]))
    rows=[row for row in rows if not entity_kind or row['entity']['kind']==entity_kind]
    rows.sort(key=lambda row: row['relation']['id'])
    return _page(rows, offset, limit, entity=_summary(entities[entity_id]), entity_kind=entity_kind,
                 policy='Candidate/stale relations cannot establish scope or confirm quantities.')
