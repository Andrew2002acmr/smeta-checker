"""Bounded safe evaluator for VOR quantity cell formulas.

Scope is the numeric quantity column only. Supported: decimal literals,
same-sheet ``A1`` references (``$``-pinned accepted), ``+ - * /``, unary
signs, parentheses and ``ROUND(value, digits)`` with ``ROUND_HALF_UP``
(Excel-compatible: ties round away from zero). All arithmetic uses
``Decimal``; ``eval`` and macros are never involved.

Anything outside that scope -- cross-sheet/external references, other
functions, ranges, text, ``^``/``%``, comparisons -- yields an explicit
``unsupported`` status naming the construct. Cycles, unresolvable or
non-numeric references, division by zero, over-long formulas and cached
values that no longer match the recomputed result yield ``error``.
Traversal depth, total reference visits, formula length, literal length,
``ROUND`` digits and result magnitude are all bounded.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, NoReturn


def has_formula_text(value: Any) -> bool:
    """An omitted calculation is allowed; malformed supplied arithmetic is not."""
    return str("" if value is None else value).strip().casefold().rstrip(".") not in {
        "", "-", "—", "–", "nan", "none", "null", "нет", "не задано", "не задана", "не требуется",
    }

MAX_FORMULA_LENGTH = 500
MAX_LITERAL_LENGTH = 40
MAX_REFERENCE_DEPTH = 25
MAX_CELL_VISITS = 64
MAX_ROUND_DIGITS = 28
MAX_EXPRESSION_DEPTH = 25
MAX_MAGNITUDE = Decimal(10) ** 15
_DECIMAL_PRECISION = 28

_ADDRESS_RE = re.compile(r"\$?[A-Za-z]{1,3}\$?[0-9]{1,7}")
_NAME_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d{1,3})?")
_MAX_EXCEL_ROW = 1048576


class _FormulaError(Exception):
    """Evaluation failure; ``unsupported`` marks out-of-scope constructs."""

    def __init__(self, message: str, *, unsupported: bool = False) -> None:
        super().__init__(message)
        self.unsupported = unsupported


def _fail(message: str, *, unsupported: bool = False) -> NoReturn:
    raise _FormulaError(message, unsupported=unsupported)


def normalize_address(raw: Any) -> str | None:
    """Return the canonical ``A1`` form or ``None`` when not an address."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper()
    if _ADDRESS_RE.fullmatch(text) is None:
        return None
    canonical = text.replace("$", "")
    row = int(re.search(r"[0-9]+", canonical).group(0))  # type: ignore[union-attr]
    if row < 1 or row > _MAX_EXCEL_ROW:
        return None
    return canonical


def _json_safe(value: Any) -> Any:
    """Coerce an openpyxl cell value into a JSON-serializable shape."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _checked(value: Decimal, what: str) -> Decimal:
    if value.is_nan() or not value.is_finite():
        _fail(f"{what} is not finite")
    if abs(value) > MAX_MAGNITUDE:
        _fail(f"{what} exceeds 10**15")
    return value


def _round_half_up(value: Decimal, digits: int) -> Decimal:
    quantum = Decimal(1).scaleb(-digits)
    try:
        return value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        _fail(f"cannot ROUND {value} to {digits} digits")


class _Parser:
    """Recursive-descent parser over one formula body (no leading ``=``)."""

    def __init__(self, text: str, resolve: Any, note_literal: Any) -> None:
        self._text = text
        depth = 0
        for char in text:
            if char in (":", "^", "%", "!", "[", "]", "<", ">", "=", "&", '"', "'"):
                _fail(f"construct {char!r} is not supported", unsupported=True)
            depth += (char == "(") - (char == ")")
            if depth > MAX_EXPRESSION_DEPTH:
                _fail(f"expression nesting exceeds {MAX_EXPRESSION_DEPTH}")
        self._resolve = resolve
        self._note_literal = note_literal
        self._pos = 0

    def parse(self) -> Decimal:
        value = self._parse_expr()
        self._skip_spaces()
        if self._pos != len(self._text):
            _fail(f"unexpected {self._text[self._pos]!r} at position {self._pos}")
        return value

    def _peek(self) -> str:
        if self._pos >= len(self._text):
            return ""
        return self._text[self._pos]

    def _skip_spaces(self) -> None:
        while self._peek() in (" ", "\t"):
            self._pos += 1

    def _parse_expr(self) -> Decimal:
        value = self._parse_term()
        while True:
            self._skip_spaces()
            op = self._peek()
            if op not in ("+", "-"):
                return value
            self._pos += 1
            rhs = self._parse_term()
            value = _checked(value + rhs if op == "+" else value - rhs, "result")

    def _parse_term(self) -> Decimal:
        value = self._parse_factor()
        while True:
            self._skip_spaces()
            op = self._peek()
            if op not in ("*", "/"):
                return value
            self._pos += 1
            rhs = self._parse_factor()
            if op == "*":
                value = _checked(value * rhs, "result")
            else:
                if rhs == 0:
                    _fail("division by zero")
                try:
                    value = value / rhs
                except (InvalidOperation, ZeroDivisionError):
                    _fail("division failed")
                value = _checked(value, "result")

    def _parse_factor(self) -> Decimal:
        self._skip_spaces()
        sign = 1
        count = 0
        while self._peek() in ("+", "-"):
            count += 1
            if count > 10:
                _fail("too many unary signs")
            if self._peek() == "-":
                sign = -sign
            self._pos += 1
            self._skip_spaces()
        value = self._parse_primary()
        return -value if sign < 0 else value

    def _parse_primary(self) -> Decimal:
        self._skip_spaces()
        char = self._peek()
        if char == "":
            _fail("unexpected end of formula")
        if char == "(":
            self._pos += 1
            value = self._parse_expr()
            self._skip_spaces()
            if self._peek() != ")":
                _fail("expected ')'")
            self._pos += 1
            return value
        if char == '"':
            _fail("text literals are not supported", unsupported=True)
        if char in ("'", "["):
            _fail("external workbook references are not supported", unsupported=True)
        if char == "^":
            _fail("power operator ^ is not supported", unsupported=True)
        if char == "%":
            _fail("percent operator % is not supported", unsupported=True)
        if char == ":":
            _fail("cell ranges are not supported", unsupported=True)
        if char == ",":
            _fail("commas are not supported outside ROUND", unsupported=True)
        if char in ("=", "<", ">"):
            _fail("comparisons are not supported")
        if char.isdigit() or char == ".":
            match = _NUMBER_RE.match(self._text, self._pos)
            if match is None:
                _fail(f"unexpected {char!r} at position {self._pos}")
            token = match.group(0)
            self._pos = match.end()
            if len(token) > MAX_LITERAL_LENGTH:
                _fail(f"numeric literal exceeds {MAX_LITERAL_LENGTH} characters")
            try:
                number = Decimal(token)
            except InvalidOperation:
                _fail(f"invalid number {token!r}")
            self._note_literal(token)
            return _checked(number, f"literal {token}")
        if char.isalpha() or char in ("_", "$"):
            match = _NAME_RE.match(self._text, self._pos)
            assert match is not None
            name = match.group(0)
            self._pos = match.end()
            self._skip_spaces()
            following = self._peek()
            if following == "(":
                return self._parse_call(name)
            if following == "!":
                _fail(
                    f"cross-sheet reference {name}!... is not supported",
                    unsupported=True,
                )
            if normalize_address(name) is not None:
                return self._resolve(name)
            _fail(f"unknown name {name!r}")
        _fail(f"unexpected {char!r} at position {self._pos}")

    def _parse_call(self, name: str) -> Decimal:
        func = name.replace("$", "").upper()
        self._pos += 1  # consume '('
        if func != "ROUND":
            _fail(f"function {name} is not supported", unsupported=True)
        first = self._parse_expr()
        self._skip_spaces()
        if self._peek() != ",":
            _fail("ROUND expects two arguments separated by ','")
        self._pos += 1
        second = self._parse_expr()
        self._skip_spaces()
        if self._peek() != ")":
            _fail("expected ')' to close ROUND")
        self._pos += 1
        if second != second.to_integral_value() or abs(second) > MAX_ROUND_DIGITS:
            _fail(f"ROUND digits must be an integer within ±{MAX_ROUND_DIGITS}")
        return _round_half_up(first, int(second))


def _coerce_plain_number(address: str, plain: Any) -> Decimal:
    if isinstance(plain, bool):
        _fail(f"reference {address} is not numeric")
    if isinstance(plain, Decimal):
        return _checked(plain, f"reference {address}")
    if isinstance(plain, int):
        return _checked(Decimal(plain), f"reference {address}")
    if isinstance(plain, float):
        if not math.isfinite(plain):
            _fail(f"reference {address} is not numeric")
        return _checked(Decimal(str(plain)), f"reference {address}")
    if plain is None:
        _fail(f"reference {address} is empty")
    _fail(f"reference {address} holds non-numeric {plain!r}")


def _is_numeric_cached(cached: Any) -> bool:
    return isinstance(cached, (int, float)) and not isinstance(cached, bool)


def _cached_mismatch(value: Decimal, cached: Any) -> bool:
    """Compare against the stored Excel result; ``None`` cached never matches stale."""
    if cached is None:
        return False
    if not _is_numeric_cached(cached) or not math.isfinite(cached):
        return True
    try:
        difference = abs(value - Decimal(str(cached)))
    except InvalidOperation:
        return True
    tolerance = Decimal("1e-12") * max(abs(value), abs(Decimal(str(cached))))
    return difference > tolerance


def snapshot_cells(ws_formulas: Any, ws_cached: Any) -> dict[str, dict[str, Any]]:
    """Build ``{address: {formula, cached, plain}}`` from two workbook views.

    Worksheets are duck-typed (``iter_rows`` plus ``ws[address]``), so this
    module never imports openpyxl; the caller loads the ``data_only=False``
    view for formulas and the ``data_only=True`` view for cached values.
    """

    cells: dict[str, dict[str, Any]] = {}
    for row in ws_formulas.iter_rows():
        for cell in row:
            address = cell.coordinate
            raw = cell.value
            formula = raw if isinstance(raw, str) and raw.startswith("=") else None
            cached = _json_safe(ws_cached[address].value)
            plain = None if formula is not None else _json_safe(raw)
            cells[address] = {"formula": formula, "cached": cached, "plain": plain}
    return cells


def calculate_cell(cell: str, cells: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Evaluate one workbook cell; always returns a JSON-safe result dict.

    Result keys: ``cell``, ``expression`` (the stored ``=...`` text or ``""``
    for plain values), ``value`` (decimal string or ``None``), ``cached_value``
    (raw stored result), ``status`` (``calculated``/``unsupported``/``error``),
    ``error`` (message or ``None``), ``cached_mismatch`` and ``dependencies``.
    Each dependency carries ``cell``/``expression``/``value``/``cached_value``/
    ``origin`` (``vor_cell`` vs ``formula_literal``)/``status``.
    """

    address = normalize_address(cell)
    result: dict[str, Any] = {
        "cell": address if address is not None else str(cell),
        "expression": "",
        "value": None,
        "cached_value": None,
        "status": "error",
        "error": None,
        "cached_mismatch": False,
        "dependencies": [],
    }
    if address is None:
        result["error"] = f"invalid cell address {cell!r}"
        return result
    if not isinstance(cells, Mapping):
        result["error"] = "no workbook snapshot provided"
        return result
    entry = cells.get(address)
    if not isinstance(entry, dict):
        result["error"] = f"cell {address} is not in the workbook snapshot"
        return result
    formula = entry.get("formula")
    cached = entry.get("cached")
    result["cached_value"] = _json_safe(cached)
    if not isinstance(formula, str) or not formula.startswith("="):
        result["expression"] = ""
        try:
            plain_value = _coerce_plain_number(address, entry.get("plain"))
        except _FormulaError as exc:
            result["error"] = str(exc)
            return result
        result["value"] = _decimal_text(plain_value)
        result["status"] = "calculated"
        return result
    result["expression"] = formula
    if len(formula) > MAX_FORMULA_LENGTH + 1:
        result["error"] = f"formula in {address} exceeds {MAX_FORMULA_LENGTH} characters"
        return result
    body = formula[1:]
    if not body.strip():
        result["error"] = f"formula in {address} is empty"
        return result

    state: dict[str, Any] = {
        "visiting": [],
        "memo": {},
        "deps": [],
        "seen_cells": set(),
        "seen_literals": set(),
        "visits": 0,
    }
    result["dependencies"] = state["deps"]

    def note_literal(token: str) -> None:
        if token in state["seen_literals"]:
            return
        state["seen_literals"].add(token)
        state["deps"].append(
            {
                "cell": None,
                "expression": token,
                "value": _decimal_text(Decimal(token)),
                "cached_value": None,
                "origin": "formula_literal",
                "status": "calculated",
            }
        )

    def resolve(raw_addr: str) -> Decimal:
        ref = normalize_address(raw_addr)
        assert ref is not None
        if ref in state["visiting"]:
            chain = " -> ".join([*state["visiting"], ref])
            _fail(f"reference cycle: {chain}")
        if len(state["visiting"]) >= MAX_REFERENCE_DEPTH:
            _fail(f"reference depth exceeds {MAX_REFERENCE_DEPTH}")
        if ref in state["memo"]:
            return state["memo"][ref]
        state["visits"] += 1
        if state["visits"] > MAX_CELL_VISITS:
            _fail(f"more than {MAX_CELL_VISITS} cell references")
        ref_entry = cells.get(ref)
        if not isinstance(ref_entry, dict):
            _fail(f"reference {ref} is outside the workbook snapshot")
        ref_formula = ref_entry.get("formula")
        if isinstance(ref_formula, str) and ref_formula.startswith("="):
            if len(ref_formula) > MAX_FORMULA_LENGTH + 1:
                _fail(f"formula in {ref} exceeds {MAX_FORMULA_LENGTH} characters")
            state["visiting"].append(ref)
            try:
                value = _Parser(ref_formula[1:], resolve, note_literal).parse()
            finally:
                state["visiting"].pop()
        else:
            value = _coerce_plain_number(ref, ref_entry.get("plain"))
        state["memo"][ref] = value
        stale = _cached_mismatch(value, ref_entry.get("cached"))
        if ref not in state["seen_cells"]:
            state["seen_cells"].add(ref)
            state["deps"].append(
                {
                    "cell": ref,
                    "expression": (
                        ref_formula
                        if isinstance(ref_formula, str)
                        else _decimal_text(value)
                    ),
                    "value": _decimal_text(value),
                    "cached_value": _json_safe(ref_entry.get("cached")),
                    "origin": "vor_cell",
                    "status": "error" if stale else "calculated",
                }
            )
        if stale:
            _fail(f"cached value in {ref} differs from calculated {_decimal_text(value)}")
        return value

    try:
        with localcontext() as ctx:
            ctx.prec = _DECIMAL_PRECISION
            computed = _Parser(body, resolve, note_literal).parse()
    except _FormulaError as exc:
        result["status"] = "unsupported" if exc.unsupported else "error"
        result["error"] = str(exc)
        return result
    except RecursionError:
        result["error"] = "formula nesting exceeds safe parser depth"
        return result
    result["value"] = _decimal_text(computed)
    if _cached_mismatch(computed, cached):
        result["cached_mismatch"] = True
        result["status"] = "error"
        result["error"] = (
            f"cached value {_json_safe(cached)} differs from "
            f"calculated {_decimal_text(computed)}"
        )
    else:
        result["status"] = "calculated"
    return result
