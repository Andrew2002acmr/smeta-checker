"""Typed relation between a technological operation and a project layer.

This permits a role-name difference only after a model explicitly selects
the relation and the physical header proves the assembly's full thickness.
It never establishes normative eligibility of the split.
"""

from normative_parameters import clean, material_fractions, measure


def technological_part(position, proposal, source, group, work_scope):
    if (
        proposal.get("scope_relation") != "technological_part"
        or proposal.get("scope_state") != "verified"
        or not group
        or not group.get("quantity")
        or not source.get("version_id")
        or not source.get("page")
        or source.get("kind") != "project"
        or clean(proposal.get("scope_quote", "")) not in clean(source.get("text", ""))
    ):
        return False
    wanted = material_fractions(work_scope)
    actual = material_fractions(proposal.get("scope_quote", ""))
    if not wanted or not actual or not actual <= wanted:
        return False
    full = measure(str(group["height"]), "см")
    facts = source.get("verified_parameters") or []
    if not any(
        f.get("parameter") == "thickness"
        and measure(f.get("value"), f.get("unit", "")) == full
        for f in facts
    ):
        return False
    # Same wording at another site cannot establish an operation's ownership.
    from normative_retrieval import terms

    provenance = position.position_source
    sections = getattr(provenance, "sections", []) if provenance else []
    title = " ".join(
        s.get("text", "") if isinstance(s, dict) else s.text for s in sections
    )
    generic = terms(
        "устройство дорожной одежды конструкции основания покрытия технического тротуара "
        "тип нижний средний верхний слой автомобильная дорога площадь работы"
    )
    site = terms(title) - generic
    return bool(site and site <= terms(proposal.get("scope_quote", "")))
