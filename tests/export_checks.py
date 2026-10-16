"""Check source navigation in exports, including historical comparison branches."""


def exports_match(excel, word, row, tracking):
    if "Где проверить" not in excel.columns:
        # Comparison scripts also run unmodified historical application versions.
        index = list(excel.iloc[:, 0].astype(str)).index(str(row.iloc[0]))
        page = str(row["[ПД] Фактическая страница"])
        return (str(excel.iloc[index]["[ПД] Фактическая страница"]) == page
                and word.tables[0].rows[index + 1].cells[5].text == page)
    position = str(row.iloc[0]).removesuffix(".0")
    indexes = [i for i, value in enumerate(excel["№"]) if str(value).removesuffix(".0") == position]
    if len(indexes) != 1:
        return False
    index = indexes[0]
    exported = excel.iloc[index]
    word_row = word.tables[0].rows[index + 1]
    pages = tracking.get("found_pages", [])
    location = "стр. " + ", ".join(map(str, pages)) if pages else "Место не установлено"
    return (word_row.cells[0].text.removesuffix(".0") == position
            and word_row.cells[2].text == exported["Статус аудита"]
            and str(exported["Где проверить"]).startswith(location)
            and location in word_row.cells[3].text
            and exported["Что не так"] in word_row.cells[3].text)
