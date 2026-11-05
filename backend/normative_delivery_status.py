"""Progress counts positions, not the number of research/dependency tasks."""


def stages(rows):
    result = {}
    active = {"pending": 1, "dispatched": 2, "running": 3}
    for row in sorted(rows, key=lambda r: r.available_at):
        index = row.payload.get("position_index")
        if type(index) is not int or index < 0:
            continue
        key = (row.run_id, index)
        old = result.get(key)
        if old not in active or active.get(row.status, 0) >= active.get(old, 0):
            result[key] = row.status
    return result
