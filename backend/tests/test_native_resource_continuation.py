"""Real native geometry: independent column admission and unchanged row guards."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import normative_resource_continuation as parser
from scripts.prepare_resource_continuation import targeted_candidates

FIXTURE = Path(__file__).parent / "fixtures/gesn27_resource_continuation_12_13.json"
ROOT = Path(__file__).resolve().parents[3] / "fact-pipeline-inputs/knowledge/road-norms"
PIN = ROOT / "releases/road-seed-function-20261105-v9/pin.json"


@pytest.fixture
def actual_geometry():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["previous"]["by_column"] = {
        int(k): v for k, v in data["previous"]["by_column"].items()
    }
    data["previous"]["columns"] = {
        int(k): v for k, v in data["previous"]["columns"].items()
    }
    native = object.__new__(parser.Native)
    native.lines = data["native_lines"]
    data["native"] = native
    data["page"] = SimpleNamespace(rect=SimpleNamespace(height=data["page_height"]))
    return data


def proposed(data, native=None, existing=None):
    return parser.extract_proposal(
        data["source"],
        data["previous"],
        data["table"],
        native or data["native"],
        13,
        data["norm_ids"],
        existing or set(),
    )


def target(rows):
    return [
        r
        for r in rows
        if r["proof"]["norm_code"] == "27-02-010-02"
        and r["resource_code"] == "04.1.02.05-0006"
    ]


def test_actual_geometry_recovers_exact_target_and_never_shifted_columns(
    actual_geometry,
):
    d = actual_geometry
    assert (
        parser.continuation_reason(
            d["previous"], d["table"], d["page"], 13, d["native"]
        )
        is None
    )
    assert parser.admitted_columns(d["previous"], d["table"]) == {
        3: "27-02-010-01",
        4: "27-02-010-02",
        5: "27-02-010-03",
    }
    rows, rejected = proposed(d)
    assert target(rows) == [d["expected_target"]]
    row = target(rows)[0]
    assert (row["quantity"], row["unit"], row["name"]) == (
        "5.9",
        "м3",
        "Смеси бетонные тяжелого бетона (БСТ), класс В15 (М200)",
    )
    assert row["status"] == "extracted_native_geometric_row_unreviewed"
    assert not any(
        r["proof"]["norm_code"] in {"27-02-010-04", "27-02-010-05"} for r in rows
    )
    assert {
        r["norm"] for r in rejected if r["reason"] == "column_geometry_changed"
    } == {"27-02-010-04", "27-02-010-05"}
    assert not any(
        r["resource_code"] == "04.1.02.04-0006" and r["norm_id"] == row["norm_id"]
        for r in rows
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("no_header", "no_adjacent_bottom_resource_header"),
        ("nonadjacent", "no_adjacent_bottom_resource_header"),
        ("unit_descriptor", "descriptor_geometry_changed"),
        ("name_descriptor", "descriptor_geometry_changed"),
        ("missing_column", "column_count_changed"),
        ("duplicate_column", "duplicate_column_identity"),
        ("own_header", "has_own_norm_header"),
        ("new_first_text", "first_row_not_a_resource"),
        ("page_top", "not_at_page_top"),
        ("overlap", "noncontiguous_column_geometry"),
    ],
)
def test_structural_and_shared_descriptor_gates_stay_strict(
    actual_geometry, change, reason
):
    d = actual_geometry
    p, t = d["previous"], d["table"]
    number = 13
    if change == "no_header":
        p = None
    if change == "nonadjacent":
        number = 14
    if change == "unit_descriptor":
        t["headers"][2]["bbox"][0] += 2
    if change == "name_descriptor":
        t["headers"][1]["bbox"][0] += 2
    if change == "missing_column":
        t["headers"].pop()
    if change == "duplicate_column":
        t["headers"].append(deepcopy(t["headers"][4]))
    if change == "own_header":
        t["headers"][4]["text"] = "27-02-010-02"
    if change == "new_first_text":
        t["headers"][0]["text"] += "\nNot a resource code"
    if change == "page_top":
        t["bbox"][1] = d["page_height"] * 0.4
    if change == "overlap":
        t["headers"][4]["bbox"][0] -= 1
    assert parser.continuation_reason(p, t, d["page"], number, d["native"]) == reason


def test_target_column_shift_rejects_only_affected_columns_without_value_remapping(
    actual_geometry,
):
    d = actual_geometry
    # Move one shared boundary, preserving the actual noncrossing grid.
    d["table"]["headers"][3]["bbox"][2] += 2
    d["table"]["headers"][4]["bbox"][0] += 2
    assert (
        parser.continuation_reason(
            d["previous"], d["table"], d["page"], 13, d["native"]
        )
        is None
    )
    rows, _ = proposed(d)
    assert not target(rows)
    assert {r["proof"]["norm_code"] for r in rows} == {"27-02-010-03"}


@pytest.mark.parametrize(
    "delta,admitted", [(0.74, True), (0.75, True), (0.76, False), (2.048, False)]
)
def test_existing_point_tolerance_is_not_widened(actual_geometry, delta, admitted):
    d = actual_geometry
    d["table"]["headers"][4]["bbox"][0] += delta
    assert (4 in parser.admitted_columns(d["previous"], d["table"])) is admitted


@pytest.mark.parametrize(
    "field,text,accepted,kind,quantity",
    [
        ("unit_bbox", "кг м3", False, None, None),
        ("unit_bbox", "", False, None, None),
        ("numeric_bbox", "2 / 3", False, None, None),
        ("numeric_bbox", "П", True, "project", None),
        ("numeric_bbox", "0,25", True, "numeric", "0.25"),
    ],
)
def test_literal_own_unit_and_numeric_or_project_cell_are_not_guessed(
    actual_geometry, field, text, accepted, kind, quantity
):
    d = actual_geometry

    class Altered:
        lines = d["native"].lines

        def read(self, box):
            return (
                text
                if list(box) == d["expected_target"]["proof"][field]
                else d["native"].read(box)
            )

    rows, _ = proposed(d, Altered())
    found = target(rows)
    assert bool(found) is accepted
    if accepted:
        assert (found[0]["quantity_kind"], found[0]["quantity"]) == (kind, quantity)


def test_duplicate_norm_resource_is_not_double_counted(actual_geometry):
    d = actual_geometry
    row = d["expected_target"]
    rows, rejected = proposed(d, existing={(row["norm_id"], row["resource_code"])})
    assert not target(rows)
    assert any(r["reason"] == "resource_already_exists_no_overwrite" for r in rejected)


@pytest.mark.parametrize(
    "where,marker",
    [
        ("inside", "Таблица ГЭСН 99-99-999"),
        ("inside", "Измеритель: 1 кг"),
        ("before", "Состав работ: другое"),
    ],
)
def test_new_definition_still_blocks_inheritance(actual_geometry, where, marker):
    d = actual_geometry

    class Altered:
        def read(self, box):
            inside = list(box) == d["table"]["bbox"]
            return marker if inside == (where == "inside") else d["native"].read(box)

    assert parser.continuation_reason(
        d["previous"], d["table"], d["page"], 13, Altered()
    ) == (
        "new_definition_inside_grid"
        if where == "inside"
        else "new_definition_before_grid"
    )


@pytest.fixture(scope="module")
def real_kb():
    if not PIN.exists():
        pytest.skip("Primary normative corpus is not installed")
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    path = ROOT / pin["database"]
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == pin["sha256"]
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        yield db


def test_full_primary_pdf_targeted_export_and_exact_review_replay(real_kb):
    data = targeted_candidates(
        real_kb,
        ROOT,
        "pdf-6aa5203d913dd5eb1538a4c2",
        12,
        13,
        norm_codes=["27-02-010-02"],
        resource_codes=["04.1.02.05-0006"],
    )
    assert (
        data["status"] == "native_candidate_requires_visual_review"
        and len(data["rows"]) == 1
    )
    row = data["rows"][0]
    assert (
        row["quantity"] == "5.9"
        and row["unit"] == "м3"
        and row["proof"]["admitted_column"] == 4
    )
    review = {**data, "status": "visually_reviewed_candidate_not_published"}
    assert parser.regenerate_reviewed(real_kb, ROOT, review) == [row]
    mutated = deepcopy(review)
    mutated["rows"][0]["quantity"] = "999"
    with pytest.raises(ValueError, match="does not exactly match"):
        parser.regenerate_reviewed(real_kb, ROOT, mutated)
    mutated = deepcopy(review)
    mutated["rows"][0]["proof"]["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source version differs"):
        parser.regenerate_reviewed(real_kb, ROOT, mutated)


def test_targeted_export_rejects_wrong_page_or_source_identity(real_kb):
    with pytest.raises(ValueError, match="adjacent"):
        targeted_candidates(real_kb, ROOT, "pdf-6aa5203d913dd5eb1538a4c2", 12, 14)
    with pytest.raises(ValueError, match="Unknown source"):
        targeted_candidates(real_kb, ROOT, "absent", 12, 13)


def test_staging_apply_is_idempotent_and_conflict_never_overwrites(
    actual_geometry, monkeypatch
):
    row = actual_geometry["expected_target"]
    monkeypatch.setattr(parser, "regenerate_reviewed", lambda *_args: [deepcopy(row)])
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE resources(id TEXT PRIMARY KEY,norm_id TEXT,resource_code TEXT,name TEXT,unit TEXT,raw_quantity TEXT,quantity TEXT,quantity_kind TEXT,status TEXT,citation_json TEXT);"
    )
    first = parser.apply_reviewed(db, Path("."), {})
    second = parser.apply_reviewed(db, Path("."), {})
    assert first["added"] == [row["id"]] and second["already_present"] == [row["id"]]
    assert db.execute("select count(*) from resources").fetchone()[0] == 1
    db.execute("UPDATE resources SET quantity=? WHERE id=?", ("999", row["id"]))
    before = db.total_changes
    with pytest.raises(ValueError, match="Existing resource conflict"):
        parser.apply_reviewed(db, Path("."), {})
    assert db.total_changes == before


def test_duplicate_raw_resource_rows_cannot_be_selected_by_review_identity(
    actual_geometry,
):
    d = actual_geometry
    duplicate = []
    for line in d["native"].lines:
        if 247.8 <= min(c["bbox"][1] for c in line) < 268.2:
            copied = deepcopy(line)
            for char in copied:
                char["bbox"][1] += 35
                char["bbox"][3] += 35
            duplicate.append(copied)
    d["native"].lines += duplicate
    rows, rejected = proposed(d)
    assert not target(rows)
    assert any(
        r["reason"] == "ambiguous_duplicate_continuation_resource" for r in rejected
    )


def test_review_duplicate_norm_resource_pair_rejected_before_any_read(actual_geometry):
    row = actual_geometry["expected_target"]
    duplicate = {**row, "id": "other-id"}
    with pytest.raises(ValueError, match="duplicate resource identity"):
        parser.regenerate_reviewed(
            None,
            Path("."),
            dict(
                status="visually_reviewed_candidate_not_published",
                rows=[row, duplicate],
            ),
        )


def test_targeted_export_verifies_source_hash_before_geometry(real_kb):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        real_kb.execute(
            "SELECT sql FROM sqlite_master WHERE name='sources'"
        ).fetchone()[0]
    )
    source = dict(
        real_kb.execute(
            "SELECT * FROM sources WHERE id=?", ("pdf-6aa5203d913dd5eb1538a4c2",)
        ).fetchone()
    )
    source["sha256"] = "0" * 64
    db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)", tuple(source.values()))
    with pytest.raises(ValueError, match="Unverified source blob"):
        targeted_candidates(db, ROOT, source["id"], 12, 13)
