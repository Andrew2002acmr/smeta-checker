"""Semantic HTML tables with original spans and source-cell provenance."""

from html import escape
import re


_FRAGMENT = re.compile(
    r'^<table border="1" cellpadding="5" cellspacing="0" data-table="([^"]*)" data-start="(\d+)" data-end="(\d+)">'
)


def body_start(table):
    return table.get(
        "header_rows",
        max(
            (
                cell["row"] + cell.get("rowspan", 1)
                for cell in table["cells"]
                if cell.get("role") == "header"
            ),
            default=0,
        ),
    )


def render_table(table, start=None, end=None):
    """Render one table, or a contiguous body range with its original header.

    Covered slots emit no extra td. A body rowspan crossing a range boundary
    is clipped, retaining the same physical source cell and its text.
    """
    first = body_start(table)
    start = first if start is None else start
    end = table["row_count"] if end is None else end
    if not first <= start <= end <= table["row_count"]:
        raise ValueError("Invalid table body range")
    cells = table["cells"]
    sources = []

    def rows(left, right, header):
        rendered = []
        for row in range(left, right):
            elements, covered = [], set()
            for cell in sorted(cells, key=lambda item: item["column"]):
                top, bottom = cell["row"], cell["row"] + cell.get("rowspan", 1)
                if not top <= row < bottom or (cell.get("role") == "header") != header:
                    continue
                column = cell["column"]
                colspan = cell.get("colspan", 1)
                covered.update(range(column, column + colspan))
                if row != max(top, left):
                    continue
                rowspan = min(bottom, right) - max(top, left)
                tag = "th" if header else "td"
                attrs = f' rowspan="{rowspan}"' if rowspan > 1 else ""
                attrs += f' colspan="{colspan}"' if colspan > 1 else ""
                # HTML whitespace wraps prose naturally; source line breaks stay in JSON.
                value = escape(" ".join(cell.get("text", "").split()))
                elements.append((column, f"<{tag}{attrs}>{value}</{tag}>"))
                if cell not in sources:
                    sources.append(cell)
            for column in range(table["col_count"]):
                if column not in covered:
                    elements.append((column, "<th></th>" if header else "<td></td>"))
            rendered.append(
                "<tr>" + "".join(text for _, text in sorted(elements)) + "</tr>"
            )
        return "".join(rendered)

    head = rows(0, first, True)
    body = rows(start, end, False)
    # Include an inherited section row when the selected body starts below it.
    ids = {key for cell in sources for key in cell.get("header_ids", [])}
    groups = [
        cell
        for cell in cells
        if cell.get("role") == "group"
        and cell["cell_id"] in ids
        and cell["row"] < start
    ]
    captions = (
        (
            "<caption>"
            + escape(" / ".join(" ".join(cell["text"].split()) for cell in groups))
            + "</caption>"
        )
        if groups
        else ""
    )
    sources.extend(cell for cell in groups if cell not in sources)
    text = (
        f'<table border="1" cellpadding="5" cellspacing="0" data-table="{escape(str(table["table_id"]), quote=True)}" data-start="{start}" data-end="{end}">'
        + captions
        + ("<thead>" + head + "</thead>" if first else "")
        + "<tbody>"
        + body
        + "</tbody></table>"
    )
    locators = [
        {
            **cell,
            "source_id": cell["cell_id"],
            "table_id": table["table_id"],
            "page": table["page"],
            "row_id": f"{table['table_id']}:r{cell['row'] + 1}",
            "column_id": f"{table['table_id']}:c{cell['column'] + 1}",
            "quality": cell.get("quality", table.get("quality", "uncertain")),
        }
        for cell in sources
    ]
    locator = {
        "page": table["page"],
        "table_id": table["table_id"],
        "row_id": f"{table['table_id']}:r{start + 1}" if end - start == 1 else "",
        "origin": table.get("origin", "native"),
        "quality": "usable"
        if table.get("quality") == "usable"
        and all(cell["quality"] == "usable" for cell in locators)
        else "uncertain",
        "members": locators,
        "header_ids": list(
            dict.fromkeys(key for cell in sources for key in cell.get("header_ids", []))
        ),
        "bbox": table.get("bbox", []),
    }
    return text, locator


def select_fragment(table, limit, score):
    """Prefer the complete table; otherwise keep one contiguous ranked range."""
    full = render_table(table)
    if len(full[0]) <= limit:
        return full
    first, last = body_start(table), table["row_count"]
    candidates = [
        (
            score(
                " ".join(
                    cell["text"]
                    for cell in table["cells"]
                    if cell["row"] <= row < cell["row"] + cell.get("rowspan", 1)
                )
            ),
            row,
        )
        for row in range(first, last)
    ]
    for _, row in sorted(candidates, key=lambda item: (-item[0], item[1])):
        selected = render_table(table, row, row + 1)
        if len(selected[0]) > limit:
            continue
        left, right = row, row + 1
        while left > first or right < last:
            options = []
            if left > first:
                options.append((left - 1, right))
            if right < last:
                options.append((left, right + 1))
            fitting = [(a, b, render_table(table, a, b)) for a, b in options]
            fitting = [
                (a, b, fragment)
                for a, b, fragment in fitting
                if len(fragment[0]) <= limit
            ]
            if not fitting:
                break
            left, right, selected = max(fitting, key=lambda item: score(item[2][0]))
        return selected
    return None


def resolve_fragment(tables, text):
    """Accept only HTML reproducible from the prepared table, never model text."""
    match = _FRAGMENT.match(text)
    if not match:
        return None
    table_id, start, end = match.groups()
    for table in tables:
        if escape(str(table["table_id"]), quote=True) != table_id:
            continue
        try:
            rendered, locator = render_table(table, int(start), int(end))
        except ValueError:
            return None
        return locator if rendered == text else None
    return None


def resolve_row(tables, fragment, quote):
    """Resolve a verbatim body row within a verified bounded table fragment."""
    match = _FRAGMENT.match(fragment)
    if not match:
        return None
    table_id, start, end = match.groups()
    for table in tables:
        if escape(str(table["table_id"]), quote=True) != table_id:
            continue
        matches = []
        for row in range(int(start), int(end)):
            rendered, locator = render_table(table, row, row + 1)
            body = re.search(r"<tbody>(.*?)</tbody>", rendered, re.S)
            if body and body.group(1) == quote and quote in fragment:
                matches.append(locator)
        return matches[0] if len(matches) == 1 else None
    return None
