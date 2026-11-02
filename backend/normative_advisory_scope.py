"""Separate one locally reproducible advisory from real evidence obligations."""

from audit_models import VorPosition
from audit_predicates import technical_specs


def partition(item, position, checks, known_claims):
    """Keep every state/reference; never restore an old downgraded conclusion.

    The bounded parser and original matching must both identify this exact
    claim as advisory. Unknown IDs, conflicts and a missing plan fail closed.
    """
    if (not isinstance(position, VorPosition) or known_claims is None
            or item.state == 'possible_mismatch'
            or not item.claim_ids or not set(item.claim_ids) <= set(known_claims)):
        return [(item, False)]
    key = 'attribute:name:unparsed'
    local = [s for s in technical_specs(position.work_name, position.comment,
        position.position_type, [], position.formula) if s.claim_id == key]
    original = [c for c in checks or [] if c.get('claim_id') == key]
    if (len(local) != 1 or local[0].required or local[0].predicate != 'advisory'
            or len(original) != 1 or original[0].get('required') is not False
            or original[0].get('state') != 'not_applicable'
            or original[0].get('reasons') != ['informational_note'] or key not in item.claim_ids):
        return [(item, False)]
    required = [claim for claim in item.claim_ids if claim != key]
    output = [(item.model_copy(update={'claim_ids': required}), False)] if required else []
    output.append((item.model_copy(update={'claim_ids': [key]}), True))
    return output
