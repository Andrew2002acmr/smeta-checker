"""Position-scoped normative assistance. Never changes a machine audit verdict."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import UUID

from human_basis import locate_position, position_key, list_basis
from normative_corpus import Corpus, decimal
from workspace_models import NormativeFacts


@lru_cache(maxsize=8)
def _corpus(root, relative, stamp, size, expected_sha):
    return Corpus(Path(root), relative=relative, expected_sha=expected_sha)


def corpus(pin=None, profile="electrical"):
    profile = (pin or {}).get("profile", profile)
    if profile not in {"electrical", "roads"}:
        raise ValueError("Неизвестный нормативный профиль")
    name = "NORMATIVE_ROAD_KB_ROOT" if profile == "roads" else "NORMATIVE_KB_ROOT"
    folder = "road-norms" if profile == "roads" else "electrical-norms"
    root = Path(os.environ.get(name, Path(__file__).resolve().parents[2] / "knowledge-base" / folder)).resolve()
    pointer = pin or json.loads((root / "CURRENT.json").read_text(encoding="utf-8"))
    path = (root / pointer["database"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Недопустимый путь нормативного выпуска")
    stat = path.stat()
    return _corpus(str(root), pointer["database"], stat.st_mtime_ns, stat.st_size, pointer.get("sha256"))


def profile_for(record):
    root = Path(os.environ.get("NORMATIVE_ROAD_KB_ROOT", Path(__file__).resolve().parents[2] / "knowledge-base/road-norms"))
    bindings = root / "project-bindings.json"
    if bindings.exists():
        mapping = json.loads(bindings.read_text(encoding="utf-8"))
        if mapping.get(str(record["project_id"])) == "roads":
            return "roads"
    return "electrical"


@lru_cache(maxsize=32)
def _source_hash(path, stamp, size):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_path(review, source_id):
    kb = corpus(review["payload"]["kb"])
    with kb.connect() as db:
        source = db.execute("SELECT sha256,designation FROM sources WHERE id=?", (source_id,)).fetchone()
    if source is None:
        raise KeyError("Нормативный источник не найден")
    path = kb.source_pdf(source_id)
    stat = path.stat()
    if _source_hash(str(path), stat.st_mtime_ns, stat.st_size) != source["sha256"]:
        raise ValueError("Контрольная сумма нормативного PDF не совпала")
    return path, source["designation"]


def project_context(context, record, index):
    position = locate_position(context, record, index)
    facts = dict(unit=position.unit)
    origins = {"unit": "Единица в исходной строке ВОР"}
    if decimal(position.volume) is not None:
        facts["quantity"] = str(decimal(position.volume))
        origins["quantity"] = "Количество в исходной строке ВОР"
    project_citations = []
    human = list_basis(context, record, position)
    for basis in human:
        project_citations.append(dict(version_id=basis["pdf_version_id"], filename=basis["filename"],
            page=basis["page"], quote=basis["quote"][:4000], origin="human"))
    # Only explicit point-valued facts are prefilled. Limits such as "до 10 кВ"
    # are not silently turned into actual values. Machine quotes stay citations.
    texts = [(position.work_name + " " + position.comment, "Текст строки ВОР")]
    texts += [(b["quote"], f"Основание, подтверждённое инженером: стр. {b['page']}") for b in human]
    patterns = {
        "voltage_kv": r"напряжени\w*\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*кВ\b",
        "mass_kg_per_m": r"(?:масса|массой)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*кг\s*/\s*м\b",
        "height_m": r"на\s+высоте\s*(\d+(?:[.,]\d+)?)\s*м\b",
        "cross_section_mm2": r"сечени\w*\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*мм[²2]",
    }
    for field, pattern in patterns.items():
        hits = [(m[1].replace(",", "."), origin) for text, origin in texts for m in re.finditer(pattern, text, re.I)]
        values = {v for v, _ in hits}
        if len(values) == 1:
            facts[field], origins[field] = hits[0]
    stored = next((i["result"] for i in record["indexed_results"] if i["position_index"] == index), {})
    pins = record["config"].get("pdf_versions") or {}
    for evidence in stored.get("evidence") or []:
        name = evidence.get("document_id") or stored.get("pdf_name", "")
        version_id = pins.get(name)
        page = evidence.get("page")
        if not version_id or type(page) is not int or page < 1:
            continue
        ref = context.repository.load_version(UUID(version_id))
        project_citations.append(dict(version_id=version_id, filename=ref.filename, page=page,
            quote=str(evidence.get("quote") or "")[:4000], origin="machine"))
    return position, NormativeFacts.model_validate(facts), origins, project_citations[:20]


def state(context, record, index):
    from copy import deepcopy
    from normative_verdict import readable_review
    position, facts, origins, citations = project_context(context, record, index)
    rows = deepcopy(context.repository.normative_reviews(record["id"], index))
    for row in rows:
        if row['kind']=='check' and row['payload'].get('position_check'):
            row['payload']['position_check']=readable_review(row['payload']['position_check'])
    kb = corpus(profile=profile_for(record))
    with kb.connect() as db:
        sources = [dict(row) for row in db.execute("SELECT s.id,s.designation,s.pages, (SELECT count(*) FROM pages p WHERE p.source_id=s.id AND length(trim(p.clean_text))>50) AS readable_pages FROM sources s ORDER BY s.kind,s.designation")]
        gaps = [dict(row) for row in db.execute("SELECT designation,reason FROM gaps LIMIT 12")]
    return dict(work_name=position.work_name, facts=facts, fact_origins=origins, project_citations=citations,
        automatic_stage=context.repository.normative_stage(record["id"], index),
        profile=profile_for(record),
        library=dict(identity=kb.identity, sources=sources, gaps=gaps, counts=kb.counts),
        latest_search=next((r for r in rows if r["kind"] == "search"), None),
        selection=next((r for r in rows if r["kind"] == "selection"), None),
        latest_check=next((r for r in rows if r["kind"] == "check"), None))


def citations(kb, refs, label=""):
    output = []
    for ref in refs or []:
        source_id, page = ref.get("source_id"), ref.get("physical_page")
        source = kb.source_index.get(source_id)
        if source and type(page) is int and 1 <= page <= source["pages"]:
            item = dict(source_id=source_id, designation=source["designation"], page=page, label=label,
                        bbox=ref.get("bbox"), coordinate_system=ref.get("coordinate_system"))
            if item not in output:
                output.append(item)
    return output


def candidate(kb, item, facts=None):
    missing = list(item["missing_facts"])
    checks = [dict(label=c["label"], state=c["state"],
        actual=None if c.get("actual") is None else str(c["actual"]),
        expected=None if c.get("expected") is None else str(c["expected"]),
        norm_quantity=c.get("norm_quantity")) for c in item["checks"]]
    if facts and facts.get("cross_section_mm2"):
        checks.append(dict(label="Сечение кабеля, мм²", state="unknown", actual=facts["cross_section_mm2"], expected="Сверить ограничения нормы по оригиналу"))
        missing.append("Сечение сохранено, но его ограничения пока не структурированы в корпусе")
    rules = []
    for rule in item.get("rules") or []:
        effect = rule["effect"]
        rules.append(dict(id=rule["id"], title=rule["title"], state=rule["state"],
            factor=effect.get("factor"), target=effect.get("target", ""), message=effect.get("message", ""),
            missing_facts=rule["missing_facts"], citations=citations(kb, rule["citations"], rule["title"])))
    if item["entity_kind"] == "clause":
        missing = ["Подтверждение редакции и области применения требования"]
    if item["entity_kind"] == "norm" and not item.get("attributes"):
        missing.append("Проверить ограничения нормы по оригиналу: параметры не структурированы")
    resources = [dict(code=r.get("resource_code") or "", name=r.get("name") or "", unit=r.get("unit") or "",
        quantity=r.get("quantity"), quantity_kind=r.get("quantity_kind") or "unparsed",
        raw_quantity=r.get("raw_quantity") or "", status=r.get("status") or "",
        citations=citations(kb, [r.get("citation") or {}], r.get("name") or ""))
        for r in (item.get("resources") or [])[:500]]
    if len(item.get("resources") or []) > 500:
        missing.append("В интерфейсе показаны первые 500 ресурсных ячеек; полный состав — в оригинале")
    status = "possible_mismatch" if any(c["state"] == "mismatch" for c in checks) else "needs_facts" if missing else "candidate"
    if any(r["state"] == "candidate" and raw["effect"].get("type") == "possible_duplicate"
           for r, raw in zip(rules, item.get("rules") or [])):
        status = "possible_duplicate"
    return dict(id=item["id"], code=item.get("code") or item.get("label") or "", title=item.get("title") or item.get("label") or "Пункт документа",
        designation=item["designation"], entity_kind=item["entity_kind"], text=str(item.get("text") or "")[:6000],
        unit_raw=(item.get("unit") or {}).get("raw") or "", unit_base=(item.get("unit") or {}).get("unit") or "",
        unit_scale=(item.get("unit") or {}).get("scale"), status=status, extraction_status=item["status"],
        edition_status=(item.get("edition") or {}).get("edition_state", "edition_not_confirmed"),
        checks=checks, missing_facts=list(dict.fromkeys(missing)),
        composition=[str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in item.get("composition") or []],
        resources=resources, rules=rules, citations=citations(kb, item.get("citations"), item.get("code") or item.get("label") or ""))


def search(context, record, index, payload, actor):
    position, initial, origins, project_citations = project_context(context, record, index)
    facts = payload.facts.model_dump()
    initial_values = initial.model_dump()
    for key, value in facts.items():
        if value != initial_values.get(key):
            origins[key] = "Значение очищено инженером" if value in {None, ""} else "Уточнение инженера при поиске"
    kb = corpus(profile=profile_for(record))
    analysis = None
    if payload.mode == "context":
        from normative_context import retrieve
        items, analysis = retrieve(context, record, index, payload, position, project_citations, kb, candidate)
    else:
        items = [candidate(kb, item, facts) for item in kb.search(payload.query, kind=payload.kind, facts=facts, limit=12)]
    checks = []
    if facts.get("project_date"):
        checks.append(f"Дата проекта {facts['project_date']} сохранена. Действующая на неё редакция источников автоматически не установлена.")
    for other in record["config"].get("position_inputs") or []:
        if other["index"] == index:
            continue
        src, own = other.get("position_source") or {}, position.position_source
        if not own or src.get("sheet") != own.sheet or src.get("sections") != [s.model_dump(mode="json") if hasattr(s, "model_dump") else s for s in own.sections]:
            continue
        if re.search(r"подключени.*(?:кабел|провод)|(?:кабел|провод).*подключени", other["work_name"], re.I):
            checks.append(f"В этом разделе ВОР есть отдельное подключение, позиция №{other.get('number') or other['index']+1}. Сверьте его с включённым составом выбранной нормы; повторный учёт не доказан.")
    snapshot = dict(work_item_id=f"{record['id']}:{index}", query=payload.query, kind=payload.kind,
        mode=payload.mode, context_analysis=analysis,
        facts=facts, fact_origins=origins, kb=kb.identity, vor_version_id=str(record["vor_version_id"]),
        position_key=position_key(position), status=items[0]["status"] if items else "needs_facts",
        candidates=items, missing_facts=[] if items else ["Уточните название, код нормы или способ выполнения"],
        checks=checks[:10], project_citations=project_citations, selected_ids=[], comment="", search_id="", requires_engineer_review=True)
    return context.repository.save_normative_review(record["id"], index, "search", snapshot, actor)


def select(context, record, index, payload, actor):
    search_row = context.repository.normative_review(record["id"], index, UUID(payload.search_id))
    position = locate_position(context, record, index)
    data = search_row["payload"]
    if search_row["kind"] != "search" or data["position_key"] != position_key(position) or data["vor_version_id"] != str(record["vor_version_id"]):
        raise ValueError("Поиск относится к другой позиции или версии ВОР")
    ids = list(dict.fromkeys(payload.norm_ids))
    if not set(ids).issubset({c["id"] for c in data["candidates"]}):
        raise ValueError("Выберите варианты из сохранённого поиска")
    checks = list(data["checks"])
    tables = [c["code"].rsplit("-", 1)[0] for c in data["candidates"] if c["id"] in ids and c["entity_kind"] == "norm"]
    if len(set(tables)) < len(tables):
        checks.append("Выбрано несколько вариантов одной таблицы. Проверьте разделение работ или объёмов; одновременное применение альтернативных вариантов не подтверждено.")
    return context.repository.save_normative_review(record["id"], index, "selection",
        {**data, "checks": checks, "selected_ids": ids, "comment": payload.comment, "search_id": search_row["id"]}, actor)
