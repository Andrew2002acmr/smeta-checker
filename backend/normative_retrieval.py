"""Content-driven page/block retrieval; no expected VOR quantity or page numbers."""
import math
import re
from collections import Counter
from collections import OrderedDict
import hashlib
from threading import RLock
from typing import Any

_CACHE = OrderedDict()
_CACHE_LOCK = RLock()
_CACHE_CHARACTERS = 8_000_000


def document_search(texts):
    """Share immutable indexes within a worker, with a bounded retained text size."""
    frozen = tuple(texts)
    identity = hashlib.sha256()
    for text in frozen:
        raw = text.encode('utf-8')
        identity.update(len(raw).to_bytes(8, 'big'))
        identity.update(raw)
    key = identity.digest()
    size = sum(map(len, frozen))
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        result = DocumentSearch(frozen)
        if size <= _CACHE_CHARACTERS:
            while _CACHE and (len(_CACHE) >= 4 or
                    sum(v.character_count for v in _CACHE.values()) + size > _CACHE_CHARACTERS):
                _CACHE.popitem(last=False)
            _CACHE[key] = result
        return result

def terms(text):
    # Share normalization with matching; "нижнего" and "нижний" must not
    # become different five-character prefixes in the normative search.
    from audit_core import _terms
    from normative_scope_tokens import scope_tokens
    from normative_code_tokens import code_tokens
    return _terms(text) | set(re.findall(r'\d+/\d+(?:[.,]\d+)?', str(text))) | scope_tokens(text) | code_tokens(text)


def blocks(text, size=2400):
    """Overlapping windows retain neighbouring headings and continuation offsets."""
    if len(text) <= size:
        return [(0, text)]
    return [(offset, text[offset:offset+size]) for offset in range(0, len(text), size-400)]


class DocumentSearch:
    def __init__(self, texts):
        self.texts = tuple(texts)
        self.character_count = sum(map(len, self.texts))
        self.rows = [(p+1, offset, text, terms(text)) for p, page in enumerate(texts) for offset, text in blocks(page)]
        counts = Counter(t for _, _, _, ts in self.rows for t in ts)
        self.weights = {t: math.log(1+len(self.rows)/n) for t, n in counts.items()}
        self.postings = {}
        self.table_rows = set()
        self.table_bonus = {}
        for index, (_, _, text, tokens) in enumerate(self.rows):
            for token in tokens:
                self.postings.setdefault(token, set()).add(index)
            if re.search(r'ведомост|объем|площад|количеств|итого|всего', text, re.I):
                self.table_rows.add(index)
                self.table_bonus[index] = 4 + min(5, len(re.findall(r'\d+[.,]\d+', text)) / 8)

    def search(self, query, *, tables=False, limit=5):
        return self.search_page(query, tables=tables, limit=limit)['hits']

    def search_page(self, query, *, tables=False, limit=5, offset=0) -> dict[str, Any]:
        """Rank the full representation; a bounded preview never proves absence."""
        if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise ValueError('Positive limit and nonnegative offset required')
        wanted = terms(query)
        candidates = set().union(*(self.postings.get(token, set()) for token in wanted))
        if tables:
            # A real schedule may lack a recognised "quantity/total" label.
            # Table-marker heuristics broaden discovery; they must not remove
            # pages that match the requested material or work itself.
            candidates.update(self.table_rows)
        ranked = []
        for index in candidates:
            page, block_offset, text, ts = self.rows[index]
            overlap = wanted & ts
            score = sum(self.weights[t] for t in overlap)
            if tables:
                score += self.table_bonus.get(index, 0)
            ranked.append((score, page, block_offset, text))
        ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
        found, seen = [], set()
        for score, page, block_offset, text in ranked:
            if page in seen:
                continue
            seen.add(page)
            found.append(dict(page=page, offset=block_offset, preview=text[:450], score=round(score, 2)))
        end = min(offset + limit, len(found))
        return dict(hits=found[offset:end], offset=offset, total_matches=len(found),
            next_offset=end if end < len(found) else None,
            truncated=end < len(found), indexed_pages=len(self.texts),
            candidate_blocks=len(candidates), scope_verified=False, absence_proven=False)
