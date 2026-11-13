"""Technical verification of the experimental Codex audit wire contract."""

import base64
import hashlib
import json
from types import SimpleNamespace

import openpyxl
import pymupdf
import pytest

from codex_contract import (
    AuditContractError,
    Citation,
    output_schema,
    position_status,
    result_metrics,
    validate_result,
)
from codex_sources import PinnedCitationVerifier, PinnedDocument
from codex_document_tool import Documents


def source(document_id="design", kind="project", page=1, sheet=None, row=None, quote="Total area 128.25 m2"):
    return dict(document_id=document_id, kind=kind, page=page, sheet=sheet, row=row, quote=quote)


def direction(status, **fields):
    return {
        "status": status,
        "parameter": "Area",
        "vor_value": None,
        "source_value": None,
        "unit": None,
        "calculation": None,
        "deviation": None,
        "conclusion": "Engineering finding.",
        "sources": [],
        "missing_facts": None,
        **fields,
    }


@pytest.fixture
def audit_files(tmp_path):
    path = tmp_path / "design.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((20, 20), "Total area 128.25 m2")
        pdf.new_page().insert_text((20, 20), "Total area 510 m2")
        pdf.save(path)
    workbook_path = tmp_path / "workbook.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "Schedule A"
    workbook.active.append(["Area", 128.25, "=57*2.25"])
    workbook.create_sheet("Schedule B").append(["Area", 15])
    workbook.save(workbook_path)
    return PinnedCitationVerifier({
        "design": PinnedDocument(path, "project"),
        "workbook": PinnedDocument(workbook_path, "vor"),
    })


@pytest.fixture
def response():
    return {
        "project_id": "project-id",
        "vor_version_id": "workbook",
        "positions": [{
            "position_index": 0,
            "sheet": "Schedule A",
            "row": 1,
            "work_name": "Install layer",
            "group": "Pavement",
            "project": direction(
                "confirmed", vor_value="128,25", source_value="128.25", unit="m2", deviation="0",
                sources=[source()],
            ),
            "normative": direction(
                "needs_facts",
                missing_facts={"required": "Edition", "reason": "The date is missing", "established": "Area confirmed"},
            ),
            "quantities": direction(
                "mismatch", vor_value="120", source_value="128.25", unit="m2", deviation="8.25",
                calculation={"expression": "57 * 2.25", "result": "128.25"},
                sources=[source("workbook", "vor", None, "Schedule A", 1, "=57*2.25")],
            ),
        }],
    }


def run_validation(response, verifier):
    return validate_result(
        json.dumps(response), project_id="project-id", vor_version_id="workbook",
        expected_rows={0: ("Schedule A", 1, "Install layer")}, verify_citation=verifier,
        vor_document_id="workbook",
    )


def test_technical_validation_preserves_independent_findings(response, audit_files):
    result = run_validation(response, audit_files)
    assert position_status(result.positions[0]) == "mismatch"
    metrics = result_metrics(result)
    assert metrics["direction_statuses"]["project"]["confirmed"] == 1
    assert metrics["direction_statuses"]["normative"]["needs_facts"] == 1
    assert metrics["direction_statuses"]["quantities"]["mismatch"] == 1
    assert metrics["verified_citations"] == 2
    assert metrics["reproducible_calculations"] == 1


def test_independent_validation_keeps_verified_directions(response, audit_files):
    position = response['positions'][0]
    position['quantities']['calculation']['operand_sources'] = [0, 0]
    position['project']['sources'][0]['quote'] = '2'  # Invalid citation schema.
    rejected = []
    result = validate_result(json.dumps(response), project_id='project-id', vor_version_id='workbook',
        expected_rows={0: ('Schedule A', 1, 'Install layer')}, verify_citation=audit_files,
        vor_document_id='workbook', keep_independent=True, on_rejected=lambda *item: rejected.append(item))
    assert result.positions[0].project.status == 'not_checked'
    assert result.positions[0].quantities.status == 'mismatch'
    assert rejected == [(0, 'project', ['schema'])]


@pytest.mark.parametrize('bad_operand', [False, True])
def test_unverified_extra_citation_keeps_only_independent_arithmetic(response, audit_files, bad_operand):
    finding = response['positions'][0]['quantities']
    finding.update(status='needs_facts', missing_facts={'required': 'Edition', 'reason': 'Missing', 'established': 'Area'})
    finding['sources'].insert(0, source(page=2))
    finding['calculation']['operand_sources'] = [0 if bad_operand else 1, 1]
    result = validate_result(json.dumps(response), project_id='project-id', vor_version_id='workbook',
        expected_rows={0: ('Schedule A', 1, 'Install layer')}, verify_citation=audit_files,
        vor_document_id='workbook', keep_independent=True)
    quantities = result.positions[0].quantities
    assert quantities.status == 'not_checked'
    assert quantities.missing_facts is None
    if bad_operand:
        assert quantities.calculation is None
        assert quantities.sources == []
    else:
        assert quantities.calculation.operand_sources == [0, 0]
        assert [citation.quote for citation in quantities.sources] == ['=57*2.25']


def test_operand_cannot_reference_unrelated_value(response, audit_files):
    position = response['positions'][0]
    position['quantities']['calculation']['operand_sources'] = [1, 0]
    position['quantities']['sources'].append(source())
    result = validate_result(json.dumps(response), project_id='project-id', vor_version_id='workbook',
        expected_rows={0: ('Schedule A', 1, 'Install layer')}, verify_citation=audit_files,
        vor_document_id='workbook', keep_independent=True)
    assert result.positions[0].quantities.status == 'not_checked'
    assert result.positions[0].project.status == 'confirmed'


def test_workbook_arithmetic_does_not_prove_project_quantity(response, audit_files):
    position = response['positions'][0]
    workbook_finding = position['quantities']
    workbook_finding['calculation']['operand_sources'] = [0, 0]
    position['project'] = {**workbook_finding, 'status': 'confirmed',
                           'sources': [*workbook_finding['sources'], source(page=2, quote='Total area 510 m2')]}
    result = validate_result(json.dumps(response), project_id='project-id', vor_version_id='workbook',
        expected_rows={0: ('Schedule A', 1, 'Install layer')}, verify_citation=audit_files,
        vor_document_id='workbook', keep_independent=True)
    assert result.positions[0].project.status == 'not_checked'
    assert result.positions[0].quantities.status == 'mismatch'


def test_missing_position_does_not_erase_valid_conclusions(response, audit_files):
    response['positions'][0]['quantities']['calculation']['operand_sources'] = [0, 0]
    missing = []
    result = validate_result(json.dumps(response), project_id='project-id', vor_version_id='workbook',
        expected_rows={0: ('Schedule A', 1, 'Install layer'), 2: ('Schedule A', 4, 'Place kerb')},
        verify_citation=audit_files, vor_document_id='workbook', keep_independent=True,
        on_missing_position=missing.append)
    assert missing == [2]
    assert position_status(result.positions[0]) == 'mismatch'
    assert position_status(result.positions[1]) == 'not_checked'
    assert result.positions[1].work_name == 'Place kerb'
    assert all(not getattr(result.positions[1], key).sources for key in ('project', 'normative', 'quantities'))


def test_open_technology_keeps_project_area_and_proved_mismatch(response, audit_files):
    row = response['positions'][0]
    row['quantities']['calculation']['operand_sources'] = [0, 0]
    row['parameter_findings'] = [{'direction': 'project', 'finding': direction(
        'needs_facts', parameter='Technology', missing_facts={
            'required': 'Construction plan', 'reason': 'Equipment approval', 'established': 'Area 128.25 m2',
        }, sources=[source()])}]
    result = validate_result(json.dumps(response), project_id='project-id', vor_version_id='workbook',
        expected_rows={0: ('Schedule A', 1, 'Install layer')}, verify_citation=audit_files,
        vor_document_id='workbook', keep_independent=True)
    position = result.positions[0]
    assert position.project.status == 'confirmed' and position.project.source_value == '128.25'
    assert position.parameter_findings[0].finding.status == 'needs_facts'
    assert position_status(position) == 'mismatch'
    metrics = result_metrics(result)
    assert metrics['confirmed_project_parameters'] == 1
    assert metrics['direction_statuses']['project']['needs_facts'] == 1


def test_recheck_inherits_only_relevant_intact_evidence_on_same_pins(response):
    from codex_worker import CodexAuditError, _inherit_evidence

    observation = {'role': 'cell', 'quote': '128.25'}
    key = hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
    position = response['positions'][0]
    position['parameter_findings'] = [{'direction': 'project', 'finding': direction(
        'confirmed', sources=[{**source(), 'evidence_id': key}])}]
    config = {'pdf_versions': {'design': 'a'}, 'codex_normative': {'release': 'r'}}
    parent = {'project_id': 'p', 'vor_version_id': 'v', 'config': config,
              'indexed_results': [{'position_index': 0, 'result': position}]}
    child = {**parent, 'config': {**config, 'codex_parent_run_id': 'bb63c11c-1c5f-4cd1-93ce-8a082ae80d3f'}}
    observations = {key: observation, 'unreferenced': {'quote': '100'}}
    context = SimpleNamespace(repository=SimpleNamespace(
        load_run=lambda _: parent, load_run_report=lambda *_: json.dumps(observations)))
    assert _inherit_evidence(context, child, {0: None}) == ([position], {key: observation})
    assert _inherit_evidence(context, child, {1: None}) == ([], {})
    observations[key] = {'role': 'cell', 'quote': '999'}
    assert _inherit_evidence(context, child, {0: None}) == ([position], {})
    child['config']['pdf_versions'] = {'design': 'b'}
    with pytest.raises(CodexAuditError, match='recheck_input_version_mismatch'):
        _inherit_evidence(context, child, {0: None})


@pytest.mark.parametrize("change", [
    lambda result: result["positions"].append(result["positions"][0]),
    lambda result: result["positions"][0].update(sheet="Schedule B"),
    lambda result: result["positions"][0]["project"]["sources"][0].update(page=2),
    lambda result: result["positions"][0]["project"]["sources"][0].update(document_id="untrusted"),
    lambda result: result["positions"][0]["project"].update(source_value="128", deviation="-0.25"),
    lambda result: result["positions"][0]["quantities"]["calculation"].update(expression="10 + 20"),
    lambda result: result["positions"][0]["quantities"]["calculation"].update(expression="len('abc')"),
    lambda result: result["positions"][0]["normative"].update(status="not_checked"),
])
def test_invalid_conclusions_are_rejected(change, response, audit_files):
    change(response)
    with pytest.raises(AuditContractError):
        run_validation(response, audit_files)


def test_workbook_citation_is_bound_to_a_single_sheet(audit_files):
    assert audit_files(Citation(**source("workbook", "vor", None, "Schedule A", 1, "128.25")))
    assert not audit_files(Citation(**source("workbook", "vor", None, "Schedule B", 1, "128.25")))


def test_pdf_quote_cannot_use_a_suffix_of_a_different_number(audit_files):
    assert not audit_files(Citation(**source(quote="25 m2")))


def test_numeric_designation_can_span_physical_pdf_lines(tmp_path):
    path = tmp_path / 'norm.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((20, 30), 'Rule 27-04-003-')
        page.insert_text((20, 45), '06 for the layer')
        pdf.save(path)
    verify = PinnedCitationVerifier({'norm': PinnedDocument(path, 'normative')})
    assert verify(Citation(**source('norm', 'normative', quote='Rule 27-04-003-06 for the layer')))
    assert not verify(Citation(**source('norm', 'normative', quote='Rule 27-04-003-07 for the layer')))


def test_json_duplicate_key_is_not_silently_replaced(response, audit_files):
    duplicate = json.dumps(response).replace('"project_id":', '"project_id": "other", "project_id":')
    with pytest.raises(AuditContractError, match="duplicate JSON key"):
        validate_result(
            duplicate, project_id="project-id", vor_version_id="workbook",
            expected_rows={0: ("Schedule A", 1, "Install layer")},
            verify_citation=audit_files, vor_document_id="workbook",
        )


def test_schema_requires_all_output_fields_and_forbids_extra_fields():
    schema = output_schema()
    for model in [schema, *schema["$defs"].values()]:
        assert model["additionalProperties"] is False
        assert set(model["required"]) == set(model["properties"])


def test_codex_calculator_uses_decimal_arithmetic(tmp_path):
    (tmp_path / 'manifest.json').write_text('{"documents": []}', encoding='utf-8')
    documents = Documents(tmp_path)
    result = documents.call('calculate', {'expression': '(431.61*0.12)*1.26', 'vor_value': '65,259'})
    assert json.loads(result['text']) == {'result': '65.259432', 'deviation': '0.000432'}
    with pytest.raises(ValueError):
        documents.call('calculate', {'expression': '1 / 0', 'vor_value': None})


def test_quoted_result_must_compare_to_pinned_workbook_value(response, audit_files):
    finding = response['positions'][0]['quantities']
    finding['unit'] = 'м2'
    finding['calculation']['operand_sources'] = [0, 0]
    result = validate_result(json.dumps(response), project_id='project-id', vor_version_id='workbook',
        expected_rows={0: ('Schedule A', 1, 'Install layer')}, verify_citation=audit_files,
        vor_document_id='workbook', keep_independent=True, expected_vor_values={0: ('128.25', 'м²')})
    assert result.positions[0].project.status == 'confirmed'
    assert result.positions[0].quantities.status == 'not_checked'


def test_short_numeric_quote_requires_addressed_pdf_cell():
    assert Citation(**source(quote='75'), evidence_id='abc').quote == '75'
    with pytest.raises(ValueError):
        Citation(**source(quote='75'))
    with pytest.raises(ValueError):
        Citation(**source(quote='м²'), evidence_id='abc')


def test_document_search_reuses_text_and_drawing_crop_stays_on_page(tmp_path):
    path = tmp_path / 'source.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=180)
        page.insert_text((30, 80), 'Foundation 24 cm')
        pdf.save(path)
    cache = tmp_path / 'index'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for query in ('Foundation', '24  cm'):
        reply = Documents.pdf(path, 'search_pdf', {'query': query}, cache_dir=cache, digest=digest)
        assert json.loads(reply['text'])['matches'][0]['page'] == 1
    assert len(list(cache.glob('*.json'))) == 1
    hits = Documents.pdf(path, 'search_pdf_terms', {'first_page': 1, 'last_page': 1,
                         'queries': ['Foundation', '24 cm']}, cache_dir=cache, digest=digest)
    assert json.loads(hits['text'])['matches'][0]['queries'] == ['Foundation', '24 cm']
    crop = Documents.pdf(path, 'view_region', {'page': 1, 'bbox': [0.1, 0.2, 0.5, 0.7]})
    pixmap = pymupdf.Pixmap(base64.b64decode(crop['data']))
    assert (pixmap.width, pixmap.height) == (720, 540)
    with pytest.raises(ValueError):
        Documents.pdf(path, 'view_region', {'page': 1, 'bbox': [0.5, 0, 0.1, 1]})


def test_document_cache_is_shared_only_by_identical_pins(tmp_path):
    cache = tmp_path / 'cache'
    for name, value in (('first', '431.61'), ('same', '431.61'), ('revised', '5303.00')):
        root = tmp_path / name
        path = root / 'inputs' / 'project-1.pdf'
        path.parent.mkdir(parents=True)
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((20, 20), f'Area {value}')
            pdf.save(path, no_new_id=True)
        (root / 'manifest.json').write_text(json.dumps({'documents': [{
            'id': 'project', 'kind': 'project', 'path': 'inputs/project-1.pdf',
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        }]}), encoding='utf-8')
        reply = Documents(root, cache).call('search_pdf', {'document_id': 'project', 'query': 'Area'})
        assert json.loads(reply['text'])['matches'][0]['text'] == f'Area {value}'
    assert len(list((cache / 'pdf-navigation-cache').glob('*.json'))) == 2


def test_raster_region_returns_coordinates_only_after_corroboration(tmp_path, monkeypatch):
    path = tmp_path / 'drawing.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page(width=300, height=180).set_rotation(90)
        pdf.save(path)
    (tmp_path / 'manifest.json').write_text('{"documents": []}', encoding='utf-8')
    documents = Documents(tmp_path)
    bbox = [0.2, 0.3, 0.4, 0.36]
    args = {'document_id': 'drawing', 'page': 1, 'bbox': bbox}
    monkeypatch.setattr(Documents, 'recognize_region', staticmethod(lambda *args, **kwargs: 'Слой 24 см'))
    finding = json.loads(documents.targeted_pdf(path, 'unused', 'ocr_region', args)['text'])
    address = json.loads((tmp_path / 'codex-evidence' / f"{finding['evidence_id']}.json").read_text('utf-8'))
    assert address['highlight'] == pytest.approx(bbox)
    assert address['quote'] == 'Слой 24 см'
    monkeypatch.setattr(Documents, 'recognize_region', staticmethod(
        lambda *args, corroborate: 'Слой 24 см' if corroborate else 'Слой 42 см'))
    assert json.loads(documents.targeted_pdf(path, 'unused', 'ocr_region', args)['text'])['status'] == 'unreadable'


def test_native_table_repeated_value_has_a_distinct_address(tmp_path):
    path = tmp_path / 'table.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=260, height=150)
        grid = page.new_shape()
        for x in (20, 120, 220):
            grid.draw_line((x, 20), (x, 110))
        for y in (20, 50, 80, 110):
            grid.draw_line((20, y), (220, y))
        grid.finish(color=(0, 0, 0), width=1, closePath=False)
        grid.commit()
        for y, (name, quantity) in zip((40, 70, 100), (('Work', 'Qty'), ('First', '75'), ('Second', '75')), strict=True):
            page.insert_text((32, y), name)
            page.insert_text((150, y), quantity)
        pdf.save(path)
    (tmp_path / 'manifest.json').write_text('{"documents": []}', encoding='utf-8')
    documents = Documents(tmp_path)
    cells = [json.loads(documents.targeted_pdf(path, 'unused', 'read_native_cell',
             {'document_id': 'pdf', 'page': 1, 'table': 1, 'row': row, 'column': 2})['text']) for row in (2, 3)]
    assert [item['quote'] for item in cells] == ['75', '75']
    assert cells[0]['evidence_id'] != cells[1]['evidence_id']
    assert cells[0]['bbox'][3] == cells[1]['bbox'][1]


def test_workbook_can_read_only_requested_columns(audit_files):
    path = audit_files.documents['workbook'].path
    rows = Documents.rows(path, {'sheet': 'Schedule A', 'first_row': 1, 'last_row': 1,
                                 'columns': ['B', 'C']})
    assert rows == [{'row': 1, 'cells': {'B': 128.25, 'C': '=57*2.25'}}]


def test_csv_tool_and_citations_share_logical_records(tmp_path):
    path = tmp_path / 'work.csv'
    path.write_text('Ведомость;\nНаименование работ;Количество\n;\nУкладка слоя;431,61\n', encoding='utf-8-sig')
    rows = Documents.rows(path, {'sheet': '', 'first_row': 2, 'last_row': 2})
    assert rows['records'] == [{'row': 2, 'cells': ['Укладка слоя', '431,61']}]
    verifier = PinnedCitationVerifier({'csv': PinnedDocument(path, 'vor')})
    assert verifier(Citation(**source('csv', 'vor', None, '', 2, 'Укладка слоя')))
    assert not verifier(Citation(**source('csv', 'vor', None, '', 1, 'Укладка слоя')))


def test_raster_table_without_separator_uses_station_row(monkeypatch):
    import pdf_targeted_cells as tables

    cells = [{'slots': [0, 0, 0, 1], 'label': 'Дорога'},
             {'slots': [1, 1, 0, 0], 'label': 'ПК'}, {'slots': [1, 1, 1, 1], 'label': 'Площадь'},
             {'slots': [2, 2, 0, 0], 'label': '0+13,37 – 0+56,10 _'},
             {'slots': [2, 2, 1, 1], 'label': '277,74'},
             {'slots': [3, 3, 0, 0], 'label': 'Всего:'}, {'slots': [3, 3, 1, 1], 'label': '277,74'}]
    monkeypatch.setattr(tables, '_grid', lambda _: [cells])

    def read_labels(_, targets):
        for item in targets:
            item['text'] = item['label']

    monkeypatch.setattr(tables, '_read_labels', read_labels)
    result, = tables._structure(None)
    assert [item['text'] for item in result['headers']] == ['Дорога', 'ПК', 'Площадь']
    assert [item['text'] for item in result['rows']] == ['0+13,37 – 0+56,10 _', 'Всего:']
    for item in cells:
        item['label'] = 'Текст'
    assert tables._structure(None) == []


def test_addressed_decimal_ocr_spacing_does_not_split_the_operand():
    from codex_contract import _citation_numbers, _decimal

    addressed = source(quote='2689, 14')
    assert _citation_numbers(Citation(**addressed, evidence_id='a'*64)) == [_decimal('2689.14')]
    assert _decimal('2689.14') not in _citation_numbers(Citation(**addressed))
    addressed['quote'] = '2689, 14, 76'
    assert _decimal('2689.14') not in _citation_numbers(Citation(**addressed, evidence_id='a'*64))


def test_addressed_ocr_decimal_survives_full_validation(response):
    position = response['positions'][0]
    position['project'].update(
        vor_value='2689.14', source_value='2689.14', deviation='0',
        calculation={'expression': '2689.14', 'result': '2689.14', 'operand_sources': [0]},
        sources=[{**source(quote='2689, 14'), 'evidence_id': 'a'*64}],
    )
    position['quantities'] = direction('not_applicable')
    options = dict(project_id='project-id', vor_version_id='workbook',
                   expected_rows={0: ('Schedule A', 1, 'Install layer')},
                   vor_document_id='workbook', keep_independent=True)
    result = validate_result(json.dumps(response), verify_citation=lambda _: True, **options)
    assert result.positions[0].project.status == 'confirmed'
    result = validate_result(json.dumps(response), verify_citation=lambda _: False, **options)
    assert result.positions[0].project.status == 'not_checked'
