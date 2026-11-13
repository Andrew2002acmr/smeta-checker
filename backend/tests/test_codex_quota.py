from codex_quota import quota_metrics, weekly_reference_usd


def snapshot(percent, *, reset=123, balance='500.5'):
    return {'buckets': {'codex': {'windows': {'primary': {
        'usedPercent': percent, 'windowDurationMins': 10080, 'resetsAt': reset,
    }}, 'credit_balance': balance}}}


def test_quota_delta_and_credit_balance_are_independent():
    metrics = quota_metrics(snapshot(2), snapshot(5, balance='500.25'), '107')
    bucket = metrics['by_bucket']['codex']
    assert bucket['windows']['primary'] == {'account_delta_percentage_points': 3,
        'estimated_usd': '3.21', 'measurement': 'observed_delta'}
    assert bucket['account_credit_balance_decrease'] == '0.25'


def test_saturated_or_reset_window_has_no_measurable_delta():
    for before, after in ((snapshot(100), snapshot(100)), (snapshot(2), snapshot(5, reset=124))):
        window = quota_metrics(before, after, '107')['by_bucket']['codex']['windows']['primary']
        assert window['account_delta_percentage_points'] is None
        assert window['estimated_usd'] is None


def test_window_reaching_100_percent_reports_lower_bound():
    window = quota_metrics(snapshot(98), snapshot(100), '107')['by_bucket']['codex']['windows']['primary']
    assert window['measurement'] == 'lower_bound'
    assert window['estimated_usd'] == '2.14'


def test_monthly_reference_is_split_between_four_weekly_windows():
    reference = weekly_reference_usd({'CODEX_AUDIT_MONTHLY_QUOTA_REFERENCE_USD': '107'})
    metrics = quota_metrics(snapshot(0), snapshot(4), reference)
    assert metrics['reference_usd_per_full_weekly_codex_window'] == '26.75'
    assert metrics['by_bucket']['codex']['windows']['primary']['estimated_usd'] == '1.07'


def test_live_document_events_survive_partial_write_without_double_counting(tmp_path):
    from codex_worker import ExecutionEvents

    path = tmp_path / 'events.jsonl'
    telemetry = ExecutionEvents()
    path.write_bytes(b'{"tool":"calculate","duration_ms":10}\n{"tool":"ocr_region",')
    telemetry.add_document_events(path)
    telemetry.add_document_events(path)
    assert telemetry.metrics()['tool_calls'] == 1
    with path.open('ab') as stream:
        stream.write(b'"duration_ms":15}\n')
    telemetry.add_document_events(path)
    assert telemetry.metrics()['tool_calls'] == 2
    assert telemetry.metrics()['calculation_duration_ms'] == 10
    assert telemetry.metrics()['ocr_duration_ms'] == 15
