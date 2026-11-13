"""Project private evidence metadata onto the existing public source contract."""

from typing import Any

from workspace_models import NormativeCheckSource


def public_sources(value: Any) -> Any:
    """Return a detached response view; saved evidence remains lossless.

    Sources and known internal calculation metadata are projected. Other DTO
    fields and invalid values retain their normal contract validation.
    """
    if isinstance(value, list):
        return [public_sources(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: public_sources(item) for key, item in value.items()}
    check = result.get('position_check')
    if isinstance(check, dict) and isinstance(check.get('sources'), list):
        check['sources'] = [{key: item for key, item in source.items() if key in NormativeCheckSource.model_fields}
            if isinstance(source, dict) else source for source in check['sources']]
    if isinstance(check, dict) and isinstance(check.get('parameter_cards'), list):
        for card in check['parameter_cards']:
            if not isinstance(card, dict):
                continue
            # The public calculation role and its explanation already express
            # a derived quantity. Keep the exact typed proof in evidence_trace
            # and storage, rather than inventing a new public numeric role.
            if card.get('role') == 'calculation_declared_allocation':
                card['role'] = 'calculation'
            for private in ('quantity_role', 'proof_limits', 'project_total'):
                card.pop(private, None)
    return result
