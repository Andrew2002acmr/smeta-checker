from unittest.mock import Mock, patch
from types import SimpleNamespace

from analysis_replay import replay_normative, store_normative


def test_fresh_mode_does_not_read_prior_normative_answers():
    with patch(
        "analysis_replay.normative_identity",
        side_effect=AssertionError("No answer cache lookup"),
    ):
        assert (
            replay_normative(
                Mock(), dict(config={"normative_fresh_model": True}), 0, Mock(), "actor"
            )
            is None
        )


def test_new_model_result_is_returned_without_first_writer_cache_override():
    saved = dict(
        id="new-review",
        payload=dict(
            position_check=dict(state="needs_facts", execution_state="complete")
        ),
    )
    with patch(
        "analysis_replay.normative_identity",
        side_effect=AssertionError("No answer cache write/replay"),
    ):
        assert (
            store_normative(
                Mock(), dict(config={"normative_fresh_model": True}), 0, Mock(), saved
            )
            is saved
        )


def test_normal_runs_still_use_answer_cache_policy():
    context = SimpleNamespace(repository=SimpleNamespace(supports_analysis_replay=True))
    for config in (
        {},
        {"normative_fresh_model": False},
        {"normative_fresh_model": "true"},
    ):
        with patch("analysis_replay.normative_identity", return_value=None) as identity:
            assert (
                replay_normative(context, dict(config=config), 0, Mock(), "actor")
                is None
            )
            identity.assert_called_once()
