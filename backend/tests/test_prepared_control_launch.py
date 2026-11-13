"""Reject a slow/misconfigured control before opening the DB or provider."""
import sys

import pytest

from scripts import run_prepared_control as control


def arguments(tmp_path, indices):
    return ['control', '--matching-run', '00000000-0000-0000-0000-000000000001',
        '--preparation', str(tmp_path), '--normative-root', str(tmp_path),
        '--normative-database', 'knowledge.sqlite', '--normative-sha256', 'a' * 64,
        '--indices', *map(str, indices), '--max-requests', '70', '--out', str(tmp_path / 'out')]


def reject_db(*args, **kwargs):
    raise AssertionError('Invalid control must not open the database')


def test_missing_native_sqlite_cache_fails_before_any_run_or_request(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path, [0]))
    monkeypatch.delenv('NORMATIVE_SQLITE_CACHE_DIR', raising=False)
    monkeypatch.setattr(control.faulthandler, 'dump_traceback_later', lambda *a, **k: None)
    monkeypatch.setattr(control, 'open_repository', reject_db)
    with pytest.raises(ValueError, match='NORMATIVE_SQLITE_CACHE_DIR'):
        control.main()
    assert not (tmp_path / 'out').exists()


def test_duplicate_selection_is_rejected_before_creating_a_control(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path, [0, 0]))
    monkeypatch.setenv('NORMATIVE_SQLITE_CACHE_DIR', str(tmp_path / 'native'))
    monkeypatch.setattr(control.faulthandler, 'dump_traceback_later', lambda *a, **k: None)
    monkeypatch.setattr(control, 'open_repository', reject_db)
    with pytest.raises(ValueError, match='Explicit selection'):
        control.main()
