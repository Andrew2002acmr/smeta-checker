"""Project private evidence metadata onto the existing public source contract."""

from typing import Any

from workspace_models import NormativeCheckSource


def public_sources(value: Any) -> Any:
    """Return a detached response view; saved evidence remains lossless.

    Only source dictionaries inside position_check are projected. Other DTO
    fields and invalid source values retain their normal contract validation.
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
    return result
