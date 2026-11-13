"""Check source navigation in the Word export."""

USER_STATUSES = ("Обнаружено расхождение", "Требуется ручная проверка",
                 "Проверка не выполнена", "ОК")


def word_row_for(word, position):
    for table_row in word.tables[0].rows[1:]:
        if table_row.cells[0].text.removesuffix(".0") == position:
            return table_row
    return None


def word_status_for(word, position):
    row = word_row_for(word, position)
    return row.cells[2].text if row is not None else ""


def word_matches(word, row, tracking):
    position = str(row.iloc[0]).removesuffix(".0")
    word_row = word_row_for(word, position)
    if word_row is None:
        return False
    pages = tracking.get("found_pages", [])
    location = "стр. " + ", ".join(map(str, pages)) if pages else "Место не установлено"
    return (word_row.cells[0].text.removesuffix(".0") == position
            and location in word_row.cells[3].text)
