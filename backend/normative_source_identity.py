"""Canonical document handles and content-bound aliases, never scope approval.

Aliases are transport identities.  Resolving one does not establish the quality
of its literal, document version, geometry, or engineering ownership: consumers
must still run their ordinary physical-source validators.
"""
from copy import deepcopy
import re

from analysis_replay import digest

_SHARED = re.compile(r"shared:(0|[1-9]\d*):([0-9a-f]{24})\Z")
_PARENT = re.compile(r"parent-(0|[1-9]\d*):(?:geometry|basis):([0-9a-f]{24})\Z")
_WORK = re.compile(r"work-(0|[1-9]\d*):([0-9a-f]{24})\Z")
_ALIAS_FIELDS = {"physical_source_id", "source_alias"}


def _transport_id(value):
    return value.startswith('shared:') or re.match(r'(?:parent|work)-\d+:', value) is not None


def _base(source, canonical):
    return {key: deepcopy(value) for key, value in source.items()
            if key not in _ALIAS_FIELDS and key != "id"} | {"id": canonical}


def _late_confirmed_context(source, canonical):
    """Validate the one allowed post-alias server overlay, including its hash."""
    context = source.get("confirmed_unit_context")
    metadata = source.get("source_alias")
    if not isinstance(context, dict) or not isinstance(metadata, dict):
        return False
    expected_binding = {key: deepcopy(source.get(key)) for key in (
        "id", "version_id", "page", "text", "rects", "origin", "numeric_verified", "unit", "unit_verified")}
    expected_binding["id"] = canonical
    return bool(context.get("provenance") == "explicit_user_confirmation"
        and isinstance(context.get("unit"), str) and context.get("unit") in {"м", "м2", "м3", "шт", "т", "кг", "га"}
        and isinstance(context.get("actor"), str) and context["actor"].strip()
        and isinstance(context.get("reason"), str) and context["reason"].strip()
        and isinstance(context.get("key"), str) and re.fullmatch(r"[0-9a-f]{64}", context["key"])
        and context.get("binding") == expected_binding
        and metadata.get("unit_context_sha256") == digest(context))


def canonical_source_id(source):
    """Resolve only a direct handle or a reproducibly content-bound alias.

    The original single-hop ``shared`` envelopes predate source_alias metadata;
    their digest remains independently checkable and is accepted. Arbitrary
    physical_source_id assertions, altered envelopes and alias chains are not.
    """
    if not isinstance(source, dict):
        return None
    current = source.get("id")
    if not isinstance(current, str) or not current:
        return None
    physical = source.get("physical_source_id")
    if physical is None:
        return current if "source_alias" not in source and not _transport_id(current) else None
    if not isinstance(physical, str) or not physical or _transport_id(physical):
        return None
    if current == physical and "source_alias" not in source:
        return physical
    parent = _PARENT.fullmatch(current) or _WORK.fullmatch(current)
    match = _SHARED.fullmatch(current) or parent
    if match is None:
        return None
    base = _base(source, physical)
    fingerprint = digest(base)
    late_context = False
    if match[2] != fingerprint[:24]:
        # attach() may add a separately confirmed contextual unit after this
        # alias was issued. Ignore no document field: validate the overlay's
        # full physical binding and its own stored fingerprint first.
        if not _late_confirmed_context(source, physical):
            return None
        fingerprint = digest({key: value for key, value in base.items() if key != "confirmed_unit_context"})
        if match[2] != fingerprint[:24]:
            return None
        late_context = True
    metadata = source.get("source_alias")
    if parent and metadata is None:
        return None  # Older parent aliases had no content-bound attestation.
    expected_metadata = dict(version=1, canonical_id=physical, canonical_sha256=fingerprint,
        donor_position=int(match[1]))
    if late_context:
        expected_metadata["unit_context_sha256"] = digest(source["confirmed_unit_context"])
    if metadata is not None and metadata != expected_metadata:
        return None
    return physical


def shared_source(source, donor, *, namespace="shared"):
    """Create a single-hop alias without mutating the saved source envelope."""
    canonical = canonical_source_id(source)
    if canonical is None or type(donor) is not int or donor < 0 or namespace not in {"shared", "geometry", "basis", "work"}:
        return None
    base = _base(source, canonical)
    fingerprint = digest(base)
    return base | dict(
        id=(f"shared:{donor}:" if namespace == "shared" else f"work-{donor}:" if namespace == "work"
            else f"parent-{donor}:{namespace}:") + fingerprint[:24],
        physical_source_id=canonical,
        source_alias=dict(version=1, canonical_id=canonical,
                          canonical_sha256=fingerprint, donor_position=donor),
    )


def source_content_sha256(source):
    """Compare complete canonical envelopes while ignoring transport wrappers."""
    canonical = canonical_source_id(source)
    return digest(_base(source, canonical)) if canonical is not None else None
