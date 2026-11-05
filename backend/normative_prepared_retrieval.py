"""Search physical table observations without promoting OCR alternatives to facts."""
from normative_retrieval import terms
from typing import Any


class PreparedTableSearch:
    def __init__(self, index):
        self.observations = []
        prepared_tables: Any = index.tables
        for page, tables in enumerate(prepared_tables or [], 1):
            for table in tables:
                for cell in table['cells']:
                    if (cell.get('geometry_validation') or {}).get('requires_source_review'):
                        continue
                    readings = [('literal_cell', cell.get('text', ''))]
                    proof = cell.get('header_refinement') or {}
                    if (cell.get('role') == 'header' and proof.get('header_id') == cell['cell_id']
                            and proof.get('bbox') == cell.get('bbox')
                            and proof.get('image_sha256')):
                        readings.extend(('ocr_alternative', ' '.join(w.get('text', '') for w in v.get('words', [])))
                            for v in proof.get('variants', []))
                    semantic=cell.get('header_line_observation') or {}
                    if (semantic.get('header_id')==cell['cell_id'] and semantic.get('bbox')==cell.get('bbox')
                            and semantic.get('image_sha256')):
                        readings.extend(('ocr_line_candidate',' '.join(w.get('text','') for w in line.get('rapid_words',[])))
                            for line in semantic.get('lines',[]))
                        readings.extend(('ocr_line_candidate',' '.join(w.get('text','') for w in variant.get('words',[])))
                            for line in semantic.get('lines',[]) for variant in line.get('best_variants',[]))
                    seen = set()
                    for kind, text in readings:
                        tokens = terms(text)
                        if not tokens or not any(any(c.isalpha() for c in t) for t in tokens) or text in seen:
                            continue
                        seen.add(text)
                        self.observations.append(dict(page=page, table_id=table['table_id'],
                            cell_id=cell['cell_id'], row=cell['row'], column=cell['column'],
                            text=text, tokens=tokens, observation_kind=kind,
                            source_bbox=list(cell.get('bbox') or []), recognition_verified=False))

    def search(self, query):
        wanted = terms(query)
        if not wanted:
            return []
        ranked = []
        for observation in self.observations:
            matched = wanted & observation['tokens']
            if not matched:
                continue
            # Only lexical discovery. Neither a matching amount nor the
            # expected VOR value is supplied to this index or its ranking.
            score = len(matched) / len(wanted) + len(matched) / max(1, len(observation['tokens']))
            ranked.append((score, observation))
        ranked.sort(key=lambda item: (-item[0], item[1]['page'], item[1]['table_id'], item[1]['cell_id']))
        # Several OCR variants of one header are one physical candidate.
        # They must not consume the preview budget and hide another table.
        anchors={}
        for score,observation in ranked:
            key=(observation['page'],observation['table_id'],observation['cell_id'])
            if key not in anchors:
                anchors[key]=dict(score=score,observation={k:v for k,v in observation.items() if k!='tokens'},readings=[])
            if len(anchors[key]['readings'])<3:
                anchors[key]['readings'].append(dict(text=observation['text'],
                    observation_kind=observation['observation_kind'],recognition_verified=False))
        by_page = {}
        for anchor in anchors.values():
            observation=anchor['observation']
            page = observation['page']
            if page not in by_page:
                by_page[page] = dict(page=page, score=anchor['score'], observations=[])
            by_page[page]['observations'].append(dict(observation,readings=anchor['readings']))
        for hit in by_page.values():
            observations=hit['observations']
            tables={}
            for observation in observations:
                tables.setdefault(observation['table_id'],[]).append(observation)
            first=[group[0] for group in tables.values()]
            remaining=[o for o in observations if o not in first]
            hit.update(observations=(first+remaining)[:6],observation_count=len(observations),
                observations_truncated=len(observations)>6)
        return list(by_page.values())


def combine(text_search, table_search, query, *, tables, offset=0, limit=5) -> dict[str, Any]:
    """Page/channel rank fusion; physical addresses remain separate from text offsets."""
    text = text_search.search_page(query, tables=tables, offset=0, limit=max(1,len(text_search.texts)))
    observed = table_search.search(query) if tables else []
    rows = {}
    for rank, hit in enumerate(text['hits'], 1):
        rows[hit['page']] = dict(hit, fusion_score=1/(60+rank))
    for rank, hit in enumerate(observed, 1):
        row = rows.setdefault(hit['page'], dict(page=hit['page'], offset=0, preview='', score=0, fusion_score=0))
        row['fusion_score'] += 1/(60+rank)
        row['table_observations'] = hit['observations']
        row['table_observation_count'] = hit['observation_count']
        row['table_observations_truncated'] = hit['observations_truncated']
    ranked = sorted(rows.values(), key=lambda row: (-row['fusion_score'], row['page']))
    end = min(offset+limit, len(ranked))
    return dict(hits=ranked[offset:end], total_matches=len(ranked), offset=offset,
        next_offset=end if end<len(ranked) else None, truncated=end<len(ranked),
        indexed_pages=len(text_search.texts), candidate_blocks=text['candidate_blocks'],
        table_observation_matches=len(observed), scope_verified=False, absence_proven=False)
