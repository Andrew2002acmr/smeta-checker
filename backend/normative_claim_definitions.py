"""Typed evidence tasks generated from bounded VOR predicates, never verdicts."""
from typing import Any


def definition(spec) -> dict[str,Any]:
    from normative_verification_contract import contract
    value=dict(claim_id=spec.claim_id,label=spec.label,expected_text=spec.expected_text,predicate=spec.predicate,
        verification=contract(spec.claim_id, spec))
    if spec.property_label=='equipment_function' and spec.required:
        value['evidence_task']=dict(kind='equipment_function_and_method_compatibility',
            subject=spec.expected_text,
            required_proofs=[
                'Буквальное подтверждение указанной функции или класса машины первичным источником.',
                'Связь функции с выбранным способом работы по применимой норме и конструкцией этого участка ПД.',
                'Соблюдение явно заданных в expected_text технических характеристик и действительных ограничений нормы.'],
            scope_boundary='Этот аспект устанавливает функцию/класс и совместимость способа. '
                'Пригодность конкретного физического экземпляра, будущие испытания и разрешение на его фактическое '
                'использование не следуют из этой формулировки. Если прочитанная норма требует отдельного '
                'условия для выбора способа, проверь его адресно и укажи цитату условия.',
            project_link_required=True,normative_applicability_separate=True,
            automatic_confirmation=False)
    if spec.property_label=='normative_instruction':
        value['evidence_task']=dict(kind='normative_condition',subject=spec.expected_text,
            required_proofs=['Точная формулировка и область применения условия в нормативе.',
                'Факты той же конструкции и участка ПД для проверки этого условия.'],
            automatic_confirmation=False)
    return value
