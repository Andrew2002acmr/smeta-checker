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
    from normative_claim_migration import EQUIPMENT_NAME
    parsed=technical_specs(position.work_name,position.comment,position.position_type,[],position.formula)
    advisory=[]
    for key in ('attribute:name:unparsed','attribute:comment:equipment_names'):
        local=[s for s in parsed if s.claim_id==key]
        original=[c for c in checks or [] if c.get('claim_id')==key]
        allowed=[['informational_note']]+([[EQUIPMENT_NAME]] if key.endswith('equipment_names') else [])
        if (key in item.claim_ids and len(local)==1 and not local[0].required and local[0].predicate=='advisory'
                and len(original)==1 and original[0].get('required') is False
                and original[0].get('state')=='not_applicable' and original[0].get('reasons') in allowed):
            advisory.append(key)
    if not advisory:
        return [(item, False)]
    required = [claim for claim in item.claim_ids if claim not in advisory]
    output = [(item.model_copy(update={'claim_ids': required}), False)] if required else []
    output.append((item.model_copy(update={'claim_ids': advisory}), True))
    return output
