"""One immutable meaning per source handle, including resumed research."""

import hashlib


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
