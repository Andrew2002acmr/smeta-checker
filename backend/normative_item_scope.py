"""Local requirement ownership; never infer child ownership from model prose."""
import hashlib
import json

TRACE='unclaimed_item_scope_v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _identity(item):
    return _digest({k:v for k,v in item.items() if k!='required'})


def optional_comment_aggregate(checks):
    aggregates=[c for c in checks or [] if c.get('claim_id')=='comment']
    if len(aggregates)!=1:
        return False
    aggregate=aggregates[0]
    if (aggregate.get('required') is not False or aggregate.get('reasons')!=['comment_predicates_evaluated']
            or aggregate.get('state') in {'contradiction','source_conflict'}):
        return False
    constituents=[c for c in checks if c.get('required',True) and any(
        str(c.get('claim_id','')).startswith(kind+':comment:') for kind in ('attribute','geometry','method','basis'))]
    return bool(constituents) and not any(c.get('state') in {'contradiction','source_conflict'} for c in constituents)


def apply_scope(original,row,known_claims,checks,trace,*,replaying=False):
    """The real claim plan remains mandatory; extra unclaimed prose is advisory.

    Legacy [] can mean either empty model claims or stripped unknown IDs. Only
    an exact server record can establish the former during frozen replay.
    """
    if original.get('state')=='possible_mismatch':
        return
    original_claims=original.get('claim_ids') or []
    if original_claims==['comment'] and optional_comment_aggregate(checks):
        row['required']=False
        return
    if original_claims or known_claims is None:
        return
    identity=_digest({**row,'required':False})
    plan=_digest(sorted(known_claims))
    if replaying:
        if original.get('required') is not False or _identity(original)!=_identity(row):
            return
        valid=any(e.get('kind')==TRACE and e.get('item_sha256')==identity
            and e.get('plan_sha256')==plan and e.get('original_claim_ids')==[]
            and e.get('original_state') in {'confirmed','needs_facts'} for e in trace or [])
        if not valid:
            return
    row['required']=False
    if not replaying and trace is not None:
        event=dict(kind=TRACE,item_sha256=identity,plan_sha256=plan,
            original_claim_ids=[],original_state=original['state'])
        if event not in trace:
            trace.append(event)
