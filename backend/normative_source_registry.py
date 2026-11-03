"""One immutable meaning per source handle, including resumed research."""

import hashlib


def full_text_handle(prefix, text):
    """Full clauses must not reuse a historical truncated clause's handle."""
    return prefix+':full:'+hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]


def merge(sources, additions):
    known = {s["id"]: s for s in sources}
    if len(known) != len(sources):
        raise ValueError("Agent source registry has duplicate identities")
    for source in additions:
        previous = known.get(source["id"])
        if previous is not None:
            if previous != source:
                raise ValueError("Source registry identity conflict")
            continue
        sources.append(source)
        known[source["id"]] = source
