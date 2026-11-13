"""Compare the consumption rate used in a VOR formula against the row comment.

The comment states the estimator's declared rate (``Расход 0,6 л/м2``); the
D-formula applies some numeric rate. A mismatch is a proved internal
contradiction of the workbook row, independent of any PD search.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

_RATE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(л\s*/\s*м\s*2|л\s*/\s*м\s*²|кг\s*/\s*м\s*2)",
    re.IGNORECASE,
)


def _comment_rates(comment: str) -> list[Decimal]:
    from audit_measurements import _decimal

    rates = []
    for match in _RATE_RE.finditer(comment or ""):
        value = _decimal(match.group(1))
        if value is not None:
            rates.append(value)
    return rates


def _formula_literals(formula_text: str) -> list[Decimal] | None:
    """Collect numeric literals via the shared workbook parser.

    Returns None when the formula uses unsupported constructs: an
    unparseable formula cannot prove a rate contradiction.
    """
    from vor_formula import _Parser

    body = (formula_text or "").strip()
    if body.startswith("="):
        body = body[1:]
    if not body:
        return []
    literals: list[Decimal] = []

    def _resolve(_token: str) -> Decimal:
        # Cell references are not rates; resolve them to zero so the parse
        # can continue and literal collection stays complete.
        return Decimal(0)

    def _note(token: str) -> None:
        try:
            literals.append(Decimal(token))
        except Exception:
            pass

    try:
        _Parser(body, _resolve, _note).parse()
    except Exception:
        return None
    return literals


def check_rate_consistency(
    *, formula_text: str, comment: str, unit: str
) -> list[dict[str, Any]]:
    """Return a finding when the formula rate contradicts the comment rate."""
    from audit_measurements import close_amounts

    _ = unit
    comment_rates = _comment_rates(comment)
    if not comment_rates:
        return []
    literals = _formula_literals(formula_text)
    if literals is None or not literals:
        return []
    findings = []
    for rate in comment_rates:
        if any(close_amounts(rate, literal) for literal in literals):
            continue
        small = [literal for literal in literals if abs(literal) < 10]
        shown = ", ".join(format(literal, "f") for literal in small or literals)
        findings.append(
            {
                "kind": "rate-comment-mismatch",
                "formula_rates": [format(literal, "f") for literal in literals],
                "comment_rate": format(rate, "f"),
                "assumption": "density=1",
                "message": (
                    f"в формуле применён расход {shown}, "
                    f"в комментарии указан {format(rate, 'f')}. "
                    "Нужно согласовать норму расхода."
                ),
            }
        )
    return findings
