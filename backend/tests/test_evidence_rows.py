"""Precise saved table evidence and equivalent standard typography."""

from types import SimpleNamespace

import pymupdf

from audit_models import ClaimKind, ClaimSpec
from audit_predicates import predicate_check
from workspace_service import _addressed_evidence
from table_context import resolve_row, render_table
from test_schedule_structure import schedule


def test_catalog_row_resolves_to_its_own_physical_cells():
    grid = schedule()
    fragment, _ = render_table(grid)
    from audit_llm import _catalog_chunks

    row = next(q for q in _catalog_chunks(fragment) if "<td>75</td>" in q)
    locator = resolve_row([grid], fragment, row)
    assert locator["row_id"] == "t1:r2"
    assert {c["text"] for c in locator["members"] if c["role"] == "cell"} == {
        "4",
        grid["cells"][5]["text"],
        "м",
        "75",
    }


def test_old_saved_table_highlights_observed_row_not_the_other_identical_name():
    def row(number, quantity):
        return f"<tr><td>{number}</td><td>Fishplate RT62</td><td>шт./т</td><td>{quantity}</td></tr>"

    quote = (
        '<table data-table="t1">'
        + row("10.3", "20/0,32")
        + row("11.3", "68/1,09")
        + "</table>"
    )
    members = []
    for r, (number, quantity) in enumerate([("10.3", "20/0,32"), ("11.3", "68/1,09")]):
        for c, text in enumerate([number, "Fishplate RT62", "шт./т", quantity]):
            members.append(
                dict(
                    row_id=f"t1:r{r + 1}",
                    column=c,
                    text=text,
                    role="cell",
                    quality="usable",
                    bbox=[c * 100, 100 + r * 100, (c + 1) * 100, 130 + r * 100],
                )
            )
    locator = dict(page=1, table_id="t1", quality="usable", members=members)
    stored = dict(
        volume_confirmed=True,
        found_volume="68",
        found_unit="шт",
        checks=[dict(facts=[dict(locators=[locator])])],
    )
    evidence = dict(page=1, quote=quote)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        selected, boxes = _addressed_evidence(
            page, evidence, stored, SimpleNamespace(work_name="Fishplate RT62")
        )
        assert "68/1,09" in selected and "20/0,32" not in selected
        assert len(boxes) == 4 and all(box.y0 == 200 for box in boxes)
        stored["volume_confirmed"] = False
        assert _addressed_evidence(
            page, evidence, stored, SimpleNamespace(work_name="Fishplate RT62")
        ) == (quote, [])


def test_gost_hyphen_and_wrapped_whitespace_do_not_change_standard_number():
    spec = ClaimSpec(
        claim_id="attribute:gost",
        kind=ClaimKind.ATTRIBUTE,
        label="ГОСТ 977-88",
        expected_text="ГОСТ 977-88",
        predicate="designation",
        required=True,
    )
    for text in ("ГОСТ-977-88", "ГОСТ- 977-88", "ГОСТ\n977 - 88"):
        check = predicate_check(spec, [dict(quote=text, source_id="s")])
        assert check.state == "confirmed"
    assert (
        predicate_check(spec, [dict(quote="ГОСТ 977-89", source_id="s")]).state
        != "confirmed"
    )
