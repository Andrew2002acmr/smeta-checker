from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from normative_service import run_corpus


def test_continuation_loads_its_pinned_release_after_current_changes():
    pin=dict(database='releases/old/knowledge.sqlite',release_id='old',sha256='a'*64,profile='roads')
    record=dict(id='run',config={})
    with patch('normative_service.profile_for',return_value='roads'), patch(
        'normative_service.corpus',return_value=SimpleNamespace(identity=pin)) as load:
        out=run_corpus(SimpleNamespace(repository=SimpleNamespace()),record,
            dict(review=dict(payload=dict(kb=pin))))
    assert out.identity==pin and record['config']['normative_kb_pin']==pin
    load.assert_called_once_with(pin,profile='roads')


def test_concurrent_winner_is_loaded_instead_of_callers_later_current_release():
    first=dict(database='releases/first/knowledge.sqlite',release_id='first',sha256='a'*64,profile='roads')
    later={**first,'release_id':'later','sha256':'b'*64,'database':'releases/later/knowledge.sqlite'}
    class Repository:
        def pin_normative_corpus(self,run_id,candidate):
            assert run_id=='run' and candidate==later
            return deepcopy(first)
    with patch('normative_service.profile_for',return_value='roads'), patch(
        'normative_service.corpus',side_effect=[SimpleNamespace(identity=later),SimpleNamespace(identity=first)]):
        record=dict(id='run',config={})
        result=run_corpus(SimpleNamespace(repository=Repository()),record)
    assert result.identity==record['config']['normative_kb_pin']==first


def test_continuation_cannot_cross_run_release_or_project_profile():
    pin=dict(profile='roads',release_id='old')
    context=SimpleNamespace(repository=Mock())
    with pytest.raises(ValueError,match='differs from run pin'):
        run_corpus(context,dict(config=dict(normative_kb_pin=dict(profile='roads',release_id='new'))),
            dict(review=dict(payload=dict(kb=pin))))
    with patch('normative_service.profile_for',return_value='electrical'), pytest.raises(
        ValueError,match='profile differs'):
        run_corpus(context,dict(config={}),dict(review=dict(payload=dict(kb=pin))))
