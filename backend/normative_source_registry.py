"""One immutable meaning per source handle, including resumed research."""

import hashlib
import json


def source_fingerprint(source):
    """Pin context exposure to the exact registered envelope, not its handle."""
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
        default=str).encode()).hexdigest()


def normative_evidence_identity(source):
    """Exact document excerpt identity, independent only of its local handle.

    Preserve *all* metadata, including permissions/quality/KB pins. A matching
    title, code, page or text prefix alone never establishes equivalence.
    """
    if (source.get('kind')!='normative' or not source.get('source_id')
            or type(source.get('page')) is not int or source['page']<1
            or not all(isinstance(source.get(k),str) and source[k] for k in ('text','label','origin'))):
        return None
    body={k:v for k,v in source.items() if k!='id'}
    return hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def same_normative_evidence(left, right, sources):
    """Compare immutable evidence sets; never infer another excerpt's validity."""
    lookup={s['id']:s for s in sources}
    if len(lookup)!=len(sources) or not left or not right:
        return False
    def identities(ids):
        if any(sid not in lookup or lookup[sid].get('kind')!='normative' for sid in ids):
            return None
        return {normative_evidence_identity(lookup[sid]) or ('exact_handle',sid) for sid in ids}
    first,second=identities(left),identities(right)
    return first is not None and first==second


def full_text_handle(prefix, text):
    """Full clauses must not reuse a historical truncated clause's handle."""
    return prefix+':full:'+hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]


def rect_order_equivalent(left, right):
    """Only a permutation of the exact same highlight rectangles is allowed."""
    if left==right:
        return True
    if left.keys()!=right.keys():
        return False
    a,b=left.get('rects'),right.get('rects')
    if not isinstance(a,list) or not isinstance(b,list):
        return False
    if not all(isinstance(r,list) and len(r)==4 and all(type(v) in {int,float} for v in r) for r in a+b):
        return False
    return {k:v for k,v in left.items() if k!='rects'}=={k:v for k,v in right.items() if k!='rects'} and sorted(a)==sorted(b)


def merge(sources, additions):
    known = {s["id"]: s for s in sources}
    if len(known) != len(sources):
        raise ValueError("Agent source registry has duplicate identities")
    for source in additions:
        previous = known.get(source["id"])
        if previous is not None:
            if not rect_order_equivalent(previous,source):
                raise ValueError("Source registry identity conflict")
            continue
        sources.append(source)
        known[source["id"]] = source
