import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pymupdf
import pytest

from pdf_tables import raster_tables


def test_table_timeout_retries_same_image_once_with_larger_budget():
    calls = []

    def extract(command, **kwargs):
        calls.append((list(command), kwargs["timeout"]))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        Path(command[-1]).write_text(json.dumps([]), encoding="utf-8")

    with pymupdf.open() as doc:
        page = doc.new_page(width=100, height=100)
        with (
            patch("pdf_tables.subprocess.run", side_effect=extract),
            patch("pdf_ruled_recovery.recover_page", return_value={"tables": []}),
        ):
            assert raster_tables(page, timeout=10) == []
    assert [budget for _, budget in calls] == [10, 20]
    assert calls[0][0] == calls[1][0]


def test_second_table_timeout_remains_an_explicit_failure():
    with pymupdf.open() as doc:
        page = doc.new_page(width=100, height=100)
        with (
            patch(
                "pdf_tables.subprocess.run",
                side_effect=subprocess.TimeoutExpired("tables", 10),
            ) as extract,
            pytest.raises(subprocess.TimeoutExpired),
        ):
            raster_tables(page, timeout=10)
    assert extract.call_count == 2


def test_non_timeout_extractor_failure_is_not_hidden_or_retried():
    with pymupdf.open() as doc:
        page = doc.new_page(width=100, height=100)
        with (
            patch(
                "pdf_tables.subprocess.run",
                side_effect=subprocess.CalledProcessError(2, "tables"),
            ) as extract,
            pytest.raises(subprocess.CalledProcessError),
        ):
            raster_tables(page, timeout=10)
    assert extract.call_count == 1
