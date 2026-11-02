from normative_agent import search_evidence


class Corpus:
    def __init__(self):
        self.calls = []

    def search(self, query, *, kind, facts, limit):
        self.calls.append(kind)
        return [dict(id=f'{kind}-{i}', entity_kind='norm' if kind == 'GESN' else 'clause')
                for i in range(limit)]


def test_work_search_returns_resource_bearing_norms_and_clauses_within_budget():
    kb = Corpus()
    hits = search_evidence(kb, 'Подгрунтовочные работы розливом битумной эмульсии', {}, limit=3)
    assert [h['entity_kind'] for h in hits] == ['norm', 'clause', 'norm']
    assert kb.calls == ['GESN', 'ALL']


def test_document_clause_and_exact_norm_queries_keep_their_scope():
    for query, kind in [('ГОСТ Р 71405-2024', 'ALL'), ('1.27.18', 'ALL'), ('27-06-035-01', 'GESN')]:
        kb = Corpus()
        assert len(search_evidence(kb, query, {}, limit=2)) == 2
        assert kb.calls == [kind]


def test_no_norm_hit_does_not_discard_clause_results():
    class ClausesOnly(Corpus):
        def search(self, query, *, kind, facts, limit):
            return [] if kind == 'GESN' else super().search(query, kind=kind, facts=facts, limit=limit)
    assert len(search_evidence(ClausesOnly(), 'покрытие', {}, limit=3)) == 3
