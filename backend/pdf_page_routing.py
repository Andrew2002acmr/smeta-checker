"""Keep unanalysed drawings separate from work-table extraction failures."""

import re

VERSION = "drawing-timeout-routing-v1"


def deferred_drawing_errors(index):
    """Defer only a timed-out table extractor on a clearly titled drawing.

    Original errors remain on the snapshot. A drawing with a quantity or
    specification table is mixed content and must not bypass its table gate.
    """
    deferred = {}
    for page, error in index.errors.items():
        text = index.texts[page]
        if not str(error).startswith("scan_table_failed:TimeoutExpired:"):
            continue
        drawing = re.search(
            r"(?im)^\s*(?:продольный\s+профиль|поперечный\s+профиль|"
            r"план\s+(?:трассы|благоустройства|планировки|организации|инженерных\s+сетей))\b",
            text,
        )
        work_table = (
            re.search(r"ведомост[ьи]\s+(?:объ[её]м|работ|материал)|спецификаци[яи]", text, re.I)
            or re.search(r"наименовани", text, re.I) and re.search(r"количеств", text, re.I)
        )
        if drawing and not work_table:
            deferred[page] = dict(
                page=page + 1, kind="drawing_analysis_pending", reason="table_timeout_on_titled_drawing",
                title=drawing[0].strip(), technical_error=str(error),
                absence_of_project_fact_not_proven=True, separate_visual_analysis_required=True,
            )
    return deferred


def coverage_for_pages(index):
    deferred = deferred_drawing_errors(index)
    return dict(
        error_pages=[int(page) + 1 for page in index.errors if page not in deferred],
        technical_error_pages=[int(page) + 1 for page in index.errors],
        deferred_visual_pages=list(deferred.values()),
    )
