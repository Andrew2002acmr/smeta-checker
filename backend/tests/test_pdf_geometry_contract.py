from copy import deepcopy

import pymupdf
import pytest

from pdf_geometry_contract import validate_tables


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_visible_bounds_are_unrotated_and_changed_geometry_cannot_keep_approval(rotation):
    with pymupdf.open() as doc:
        page = doc.new_page(width=100, height=200)
        page.set_rotation(rotation)
        inside = dict(cell_id='good', role='cell', bbox=[10, 20, 80, 40], text='17', numeric_verified=True)
        outside = dict(cell_id='bad', role='cell', bbox=[90, 20, 120, 40], text='19', numeric_verified=True)
        gone = dict(cell_id='gone', role='cell', bbox=[120, 20, 140, 40], text='21', numeric_verified=True)
        t = dict(table_id='t', bbox=[0, 0, 140, 190], cells=[inside, outside, gone])
        old = deepcopy(inside)
        result = validate_tables(page, [t])
        assert inside == old
        assert outside['bbox'] == [90, 20, 100, 40]
        assert outside['raw_bbox_before_validation'] == [90, 20, 120, 40]
        assert gone['bbox'] == []
        assert not outside['numeric_verified'] and not gone['numeric_verified']
        assert result['unlocatable_boxes'] == 1
        assert page.rotation == rotation
