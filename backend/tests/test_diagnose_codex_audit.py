from copy import deepcopy

from scripts.diagnose_codex_audit import diagnose


def test_preserves_original_parameter_and_all_rejected_citations():
    unfinished = dict(status='not_checked', parameter='Объём', conclusion='Проверка не завершена.')
    rejected = dict(unfinished, parameter='Дополнительный вопрос — типоразмер', sources=[
        dict(kind='normative', document_id='norm:incomplete'),
        dict(kind='normative', document_id='norm:' + 'a' * 64),
    ])
    raw = dict(project_id='project', vor_version_id='vor', positions=[dict(
        position_index=0, sheet='Sheet1', row=18, work_name='Работа', project=unfinished,
        normative=unfinished, quantities=unfinished,
        parameter_findings=[dict(direction='normative', finding=rejected)],
    )])
    stored = deepcopy(raw)
    stored['positions'][0]['parameter_findings'] = []
    events = [dict(event='validation.citation_rejected', position_index=0,
                   direction='normative:1', citation_index=index) for index in (0, 1)]

    report = diagnose(raw, stored, events, {})

    assert report['summary']['parameter_findings_total'] == 1
    finding = report['positions'][0]['findings'][-1]
    assert finding['agent'] == rejected and finding['persisted'] is None
    assert finding['question_role'] == 'additional_reported_by_agent'
    assert finding['backend_causes'] == ['citation_rejected']
    sources = report['rejections'][0]['rejected_sources']
    assert [source['index'] for source in sources] == [0, 1]
    assert sources[0]['classification'] == 'malformed_pinned_document_id'
