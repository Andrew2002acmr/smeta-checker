"""Source responsibilities for VOR claims; routing is never a verdict.

VOR is a proposal, PD supplies project constraints, norms supply conditions,
and a calculation binds operands. An extra VOR property is not automatically
a project requirement, an error, or an informational note.
"""

import ast
import re
from typing import Any

VERSION = "verification-relations-v4"


def contract(claim_id, spec=None):
    prop = getattr(spec, "property_label", "")
    advisory = spec is not None and not spec.required and spec.predicate == "advisory"
    if claim_id in {
        "presence",
        "quantity",
        "unit",
        "thickness",
        "input:area",
    } or claim_id.startswith("geometry:"):
        family, relation = "project_requirement", "same_subject_and_project_value"
        routes = ["project"]
    elif prop == "normative_instruction" or claim_id.startswith(
        ("normative:", "method")
    ):
        family, relation = "normative_condition", "condition_applies_to_project_facts"
        routes = ["project", "normative"]
    elif claim_id.startswith(("derived:", "basis:", "workbook:", "related:")):
        family, relation = "calculation_basis", "operands_units_and_derivation"
        routes = ["workbook", "project", "normative_or_explicit_estimator_basis"]
    elif claim_id.startswith("attribute:"):
        family, relation = "vor_refinement", "satisfies_project_constraints"
        routes = ["project", "property_definition_or_compatibility_basis"]
    else:
        family, relation = "unclassified", "resolve_claim_meaning"
        routes = ["project", "normative"]
    if advisory:
        relation = "informational_only"
    return dict(
        version=VERSION,
        family=family,
        relation=relation,
        property=prop,
        source_routes=routes,
        project_literal_required=family == "project_requirement",
        required=getattr(spec, "required", True),
        automatic_confirmation=False,
        missing_project_literal_is_mismatch=False,
    )


NORMATIVE_SCOPE_INSTRUCTIONS = """
normative_review_scope и verification_plan задают предмет normative:applicability.
Для работы это техническая применимость нормы способа выполнения работы.
Различай прямое предписание технической части применять таблицу к классу материалов
и физическую эквивалентность обозначений материалов. Если такое предписание есть,
процитируй его и не требуй эквивалентности типов ради самого направления на таблицу.
Конкретная поднорма, её условия, расход и коэффициенты проверяются отдельно;
предписание технической части не подтверждает их автоматически.
Для самостоятельного материала normative:applicability ОБЯЗАТЕЛЕН и относится к
material_specification_and_quantity_basis: докажи соответствие материала собственной
конструкции ПД, основание своего предмета количества, единицы и заявленные поправки.
quantity_subject разделяет project_physical_volume (проектный физический объём),
project_volume_with_declared_adjustment (объём с явно названной поправкой),
normative_total_consumption (явно заявленный полный расход) и unresolved.
Рамка проверки ВОР — проектные физические объёмы, включая явно названные поправки.
Метаданные scope_classification_only не доказывают числа, геометрию, свойства или коэффициенты.
Для area×rate с результатом массы не называй расчёт геометрическим объёмом;
если расход в литрах переводится в массу, отдельно докажи плотность.
Проверки потерь и коэффициентов имеют разный предмет: Купл не означает потери.
Полезный проектный объём не удостоверяет суммарный сметный расход ресурса П.
Для normative_total_consumption установи нормативные потери/отходы и их учёт;
для физического объёма не требуй доказать полноту чужого суммарного расхода.
Если ВОР прямо утверждает полный расход или учёт потерь, это обязательство сохраняется.
Ссылка нормы на ресурс П сама по себе не превращает физический объём ВОР в полный расход.
При unresolved сначала установи предмет количества; не объявляй ни gross, ни нулевые потери доказанными.
Подтверждение только марки, только геометрии или только арифметики не закрывает весь аспект.
normative_work_owner — навигация к связанной работе, а не предмет нормативного вывода
о самостоятельном материале. При независимом проектном количестве или геометрическом
расчёте материала не требуй выбора нормы способа родителя, её машины, ветви разбиения
или unit_scale для подтверждения материала. Это не снимает проверки проектной
принадлежности слоя, распределения материала между операциями и всех заявленных коэффициентов.
Потери обязательны в пределах явно заявленного суммарного расхода, а не как
дополнительное условие подтверждения полезного проектного объёма.
Если расход материала получен из ресурса рабочей нормы, докажи применимость именно
этой нормы и её условий к конструкции, буквальный расход ресурса и пересчёт с её измерителя.
Непроверенную зависимость от нормы нельзя заменить подтверждённой геометрией.
quantity выбирай по явно переданному расчётному контракту и роли проектного факта:
собственный результат материала, площадь-вход, полный проектный объём или иной
операнд формулы не взаимозаменяемы. Не назначай всем материалам площадь родителя.
Если формула распределяет полный проектный объём между операциями, отдельно докажи
общий объём, принадлежность материала и заявленное распределение; структура ВОР
и арифметическое совпадение суммы сами по себе распределение не подтверждают.
Для явно связанных технологических операций различай плановый разнос однородного
материала и утверждение о физическом объёме каждой части. Полный проектный итог,
его область, полнота группы, одинаковые свойства/единицы/поправки и отсутствие
двойного учёта проверяются вместе; доказательство распределения не устанавливает площади работ.
При такой проверке quantity может содержать буквальный полный объём ПД в м3;
сервер отдельно проверит расчётное распределение по формулам ВОР. Не подставляй
число части из ВОР в цитату ПД и не называй его прямым проектным измерением.
"""

UNIT_SCALE_INSTRUCTIONS = """
Для самостоятельной работы выбери применимую норму и её буквальный измеритель.
Для материала unit_scale извлекай только когда расчёт использует измеритель выбранной рабочей нормы.
Для независимого количества материала и типизированного входа площади input:area
unit_scale не обязателен: единица площади из ПД не является измерителем нормы.
Если передаёшь unit_scale, используй только буквальную строку «Измеритель» выбранной
нормы из источника kind=normative; единица ресурса и число в ПД её не заменяют.
Найди строку через read_source; при отсутствии не подставляй 1.
scope_state=verified и scope_quote связывают измеритель с однозначно выбранной нормой,
но не подтверждают её техническую применимость. Условия нормы, от которой зависит
расход, проверяй в normative:applicability материала; способ родителя без такой
зависимости проверяется отдельно для самой работы.
Контракт может содержать только quantity_subject/proof_limits без project_input;
это не создаёт роль площади и не меняет единицу собственного результата.
При наличии project_input различай output_unit и project_input.unit: площадь
расчётного входа не становится готовым количеством материала или мерой рабочей нормы.
"""


def quantity_subject(position) -> dict[str, Any]:
    """Describe the VOR quantity being audited; never certify its operands.

    The audit frame is physical project quantities. A total resource claim must
    be explicitly declared by the VOR; a normative P reference alone cannot
    silently change that frame. Dimensioned rates retain their density limits.
    """
    from normative_workbook_rate import analyze

    text = " ".join(
        str(getattr(position, key, "") or "") for key in ("work_name", "comment")
    )
    text = re.sub(r"\s+", " ", text).casefold().replace("ё", "е")
    gross = bool(
        re.search(
            r"(?:полный|суммарный|общий)\s+(?:сметный\s+)?расход|"
            r"с\s+учетом\s+(?:трудноустранимых\s+)?(?:потерь|отходов)",
            text,
        )
    )
    recipe = analyze(position)
    expression = (getattr(position, "calculation", None) or {}).get(
        "expression"
    ) or getattr(position, "formula", "")
    bounded_product = (
        isinstance(expression, str)
        and len(expression) <= 250
        and bool(re.fullmatch(r"[0-9.,\s*/()=×]+", expression))
    )
    if bounded_product:
        try:
            tree = ast.parse(
                expression.strip().lstrip("=").replace(",", ".").replace("×", "*"),
                mode="eval",
            )
            bounded_product = len(list(ast.walk(tree))) <= 30 and all(
                isinstance(
                    n, (ast.Expression, ast.BinOp, ast.Mult, ast.Div, ast.Constant)
                )
                and (
                    not isinstance(n, ast.Constant)
                    or isinstance(n.value, (int, float))
                    and n.value > 0
                )
                for n in ast.walk(tree)
            )
        except SyntaxError, ValueError:
            bounded_product = False
    volume = str(getattr(position, "unit", "")).casefold().replace(
        "³", "3"
    ).strip().rstrip(".") in {"м3", "m3"}
    adjustment = bool(
        re.search(
            r"купл|коэффициент[^.;]*уплотнен|коэффициент\s+(?:запаса|расхода)", text
        )
    )
    density_required = bool(recipe and recipe["density_required"])
    if gross:
        subject = "normative_total_consumption"
    elif volume and not density_required and (not expression or bounded_product):
        subject = (
            "project_volume_with_declared_adjustment"
            if adjustment
            else "project_physical_volume"
        )
    else:
        subject = "unresolved"
    return dict(
        quantity_subject=subject,
        quantity_subject_basis=dict(
            audit_frame="vor_project_physical_quantities",
            output_unit=str(getattr(position, "unit", "")),
            dimensioned_rate=recipe["rate_unit"] if recipe else "",
            bounded_numeric_recipe=bounded_product,
            explicit_adjustment=adjustment,
            explicit_total_claim=gross,
        ),
        losses_required_when="declared_quantity_is_total_normative_resource_consumption",
        proof_limits=dict(
            scope_classification_only=True,
            numbers_verified=False,
            geometry_verified=False,
            properties_verified=False,
            adjustments_verified=False,
            allocation_verified=False,
            density_required=density_required,
            total_resource_consumption_verified=False,
            losses_included_verified=False,
            zero_losses_verified=False,
        ),
    )


def review_scope(position):
    material = getattr(position, "position_type", "").strip().casefold() == "материал"
    return dict(
        subject=(
            "material_specification_and_quantity_basis"
            if material
            else "work_norm_applicability"
        ),
        required=True,
        work_cost_method_is_separate=material,
        **(quantity_subject(position) if material else {}),
        work_norm_conditions_required_when=(
            "quantity_depends_on_work_norm_resource_rate"
            if material
            else "work_norm_selected"
        ),
        unit_scale_required_when=(
            "calculation_uses_selected_work_norm_measure"
            if material
            else "work_norm_selected"
        ),
    )


def _comment_layer_thresholds(position, specs):
    """Route only a full, explicit two-layer condition; never prove its premise.

    This is the bounded conditional shape used by layer_condition_proof. A
    standalone ClaimSpec/definition has no full comment and keeps its existing
    routing; only the position-aware verification plan can bind this context.
    """
    comment = str(getattr(position, "comment", "") or "")
    conditions = list(
        re.finditer(
            r"при\s+толщин\w*[^.!?]{0,220}?(?P<operator>более|свыше|не\s+менее|не\s+более)\s*"
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мм|см|м)\b\s+"
            r"(?:(?:его|е[её]|их)\s+)?(?:(?:следует|необходимо)\s+)?"
            r"(?P<action>нормир\w*\s+в\s+два\s+слоя)",
            comment,
            re.I,
        )
    )

    def clean(text):
        return re.sub(r"\s+", " ", text).strip().casefold()

    instructions = [
        s
        for s in specs.values()
        if s.property_label == "normative_instruction"
        and s.claim_id.startswith("basis:comment:")
    ]
    found = {}
    for spec in specs.values():
        if (
            not spec.claim_id.startswith("geometry:comment:thickness:")
            or spec.property_label != "thickness"
            or spec.predicate != "measurement"
        ):
            continue
        matches = [m for m in conditions if clean(spec.label) in clean(m[0])]
        if len(matches) != 1:
            continue
        match = matches[0]
        related = [
            s.claim_id
            for s in instructions
            if clean(s.label).startswith(clean(match["action"]))
            and clean(s.label) in clean(comment)
        ]
        if len(related) != 1:
            continue
        operator = clean(match["operator"])
        found[spec.claim_id] = dict(
            kind="normative_condition",
            subject=match[0],
            threshold=dict(
                operator={
                    "более": "gt",
                    "свыше": "gt",
                    "не менее": "ge",
                    "не более": "le",
                }[operator],
                operator_text=match["operator"],
                value=match["value"],
                unit=match["unit"],
            ),
            related_claim_ids=related,
            comment_context=dict(field="position.comment", quote=comment),
            required_proofs=[
                "Буквальное условие норматива с тем же оператором и порогом.",
                "Фактическая толщина той же конструкции ПД и применимость полной предпосылки условия.",
            ],
            scope_boundary="Порог комментария не является требованием буквальной толщины ПД. "
            "Сохрани роль слоя и контекст нормирования в два слоя; числовое сравнение "
            "не подтверждает применимость ветви, выбранную норму или остальные условия.",
            automatic_confirmation=False,
        )
    return found


def plan(position, claim_ids):
    from audit_core import build_claim_specs

    specs = {spec.claim_id: spec for spec in build_claim_specs(position)}
    result: list[dict[str, Any]] = [
        dict(
            claim_id=claim,
            expected_text=getattr(specs.get(claim), "expected_text", ""),
            verification=contract(claim, specs.get(claim)),
        )
        for claim in sorted(claim_ids)
    ]
    thresholds = _comment_layer_thresholds(position, specs)
    for task in result:
        if task["claim_id"] in thresholds:
            task["verification"].update(
                family="normative_condition",
                relation="condition_applies_to_project_facts",
                source_routes=["project", "normative"],
                project_literal_required=False,
            )
            task["evidence_task"] = thresholds[task["claim_id"]]
    if getattr(position, "position_type", "").strip().casefold() == "материал":
        for task in result:
            if task["claim_id"] == "normative:applicability":
                task["verification"].update(
                    **review_scope(position),
                    required_relation="Соответствие материала проектной конструкции; основание собственного "
                    "количества в переданном quantity_subject, единиц, заявленных коэффициентов и распределения. "
                    "Потери и отходы обязательны для явно заявленного суммарного расхода; "
                    "физический объём не подтверждает полноту расхода П или нулевые потери. Условия нормы способа "
                    "родительской работы обязательны для расхода из этой нормы, а не для независимой геометрии.",
                )
    return result


def definitions_for_plan(specs, tasks):
    """Reuse one position-aware plan for the definitions in the same packet."""
    from copy import deepcopy
    from normative_claim_definitions import definition

    thresholds = {
        task["claim_id"]: task
        for task in tasks
        if "threshold" in task.get("evidence_task", {})
    }
    definitions = [definition(spec) for spec in specs]
    for value in definitions:
        task = thresholds.get(value["claim_id"])
        if task:
            value["verification"] = deepcopy(task["verification"])
            value["evidence_task"] = deepcopy(task["evidence_task"])
    return definitions


INSTRUCTIONS = """
verification_plan разделяет ответственность источников, а не ослабляет проверку:
project_requirement: установи конструкцию, участок, физический слой, свойство и
единицу в ПД; сопоставь с ВОР напрямую либо через доказанное технологическое разбиение.
normative_condition: найди точное условие нормы и проверь его по фактам того же
объекта ПД/ПОС. ПД не обязан дословно повторять нормативный способ или машину.
calculation_basis: проверь исходные операнды, перевод единиц, расход/коэффициент
и арифметику раздельно. Результат расчёта не обязан иметь отдельную строку ПД.
Для самостоятельной строки материала разделяй его спецификацию и физический
расчёт количества от сметной нормы способа выполнения родительской работы.
Не переноси условие выбора машины или технологического разбиения сметной нормы
на независимый геометрический объём материала. Если количество зависит от
расхода этой нормы, условие её применимости остаётся обязательным для расхода.
Структурный material_owner или relation_basis устанавливает только связь строк
ВОР: принадлежность материала той же конструкции ПД ещё требуется доказать.
vor_refinement: сначала извлеки действительные требования ПД к материалу, затем
свяжи дополнительную характеристику ВОР с тем же свойством/шкалой по определению
или применимому нормативу. Если это разные независимые свойства компонента и
готовой смеси, докажи их смысл и сохранение проектных характеристик в заявленной
спецификации; равенство разных шкал не требуется. Отсутствие марки ВОР в ПД само по себе не требует
замены материала. Обозначения разных свойств не объявляй эквивалентными по буквам.
Для совместимости заявленной спецификации не требуй испытаний будущей партии,
если их проведение или фактические результаты не входят в проверяемое утверждение.
Определения свойств и их ограничения получай из источников указанного документа;
перенос определения из другого стандарта требует доказанной ссылки или связи.
Обязательные свойства ПД, специальные условия стандарта и реальные противоречия
сохраняют свои проверки.
Дополнение не подтверждается автоматически: если совместимость не доказана,
оставь needs_facts и назови недостающее отношение, свойство и основание.
Не превращай дополнительное уточнение в необязательное только по его происхождению.
Противоречие требует несовместимых значений одного свойства, шкалы и области
применения. Ненайденное обозначение, неполная выдержка и нераспознанный символ
не доказывают отсутствие требования в документе.
"""

INSTRUCTIONS += NORMATIVE_SCOPE_INSTRUCTIONS + UNIT_SCALE_INSTRUCTIONS
