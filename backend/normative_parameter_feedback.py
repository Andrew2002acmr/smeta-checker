"""Mechanical citation feedback, independent of engineering scope decisions."""
from normative_parameters import clean, measure, quoted, unit_quoted


def header_parameter_verified(proposal,source):
    """A verified body-cell amount does not verify an OCR header's thickness."""
    if (proposal.get('parameter')!='thickness' or ':cell:' not in source.get('id','')
            or not source.get('origin','').startswith('ocr')):
        return True
    value=measure(proposal.get('value'),proposal.get('unit',''))
    return bool(value and any(f.get('parameter')=='thickness'
        and measure(f.get('value'),f.get('unit',''))==value for f in source.get('verified_parameters',[])))


def literal_parameter_issues(proposal, source):
    """Inspect the selected source only; never find a replacement or change scope.

    An explicitly unresolved parameter without a source is an honest missing
    fact. A supplied source/quote, however, must still be a literal extraction.
    """
    prefix = 'Параметр '+proposal.parameter+': '
    hint = (' Исправь оформление извлечённого факта по literal parameter_candidates '
        'или quantity_parameter_candidates; не подставляй количество ВОР. '
        'Это не доказательство ошибки ВОР и не подтверждение области применения.')
    if source is None:
        if not proposal.source_id and proposal.scope_state != 'verified':
            return []
        return [prefix+'указанный источник отсутствует.'+hint]
    wanted_kind = 'project' if proposal.parameter in {'quantity', 'thickness'} else 'normative'
    if source.get('kind') != wanted_kind:
        return [prefix+'тип выбранного источника не соответствует параметру.'+hint]
    if not header_parameter_verified(proposal.model_dump(),source):
        return [prefix+'проверка числа физической ячейки не подтверждает толщину в её OCR-заголовке; '
            'нужен независимо проверенный заголовок или нативное описание того же слоя.'+hint]
    if not quoted(proposal.value, proposal.quote, source['text']):
        return [prefix+'число/цитата не принадлежат указанному источнику.'+hint]
    from normative_confirmed_units import unit_confirmed
    contextual_unit = proposal.parameter == 'quantity' and unit_confirmed(source, proposal.unit)
    if proposal.parameter in {'quantity', 'thickness'} and (not proposal.unit
            or not measure(proposal.value, proposal.unit)
            or not (unit_quoted(proposal.unit, proposal.quote) or contextual_unit)):
        return [prefix+'единица отсутствует в буквальной цитате числа.'+hint]
    if (proposal.parameter=='quantity' and source.get('unit_verified')
            and (not measure('1',source.get('unit',''))
                or measure('1',proposal.unit)!=measure('1',source['unit']))):
        return [prefix+'единица не совпадает с независимо подтверждённой единицей физического столбца; '
            'упоминание другой единицы в заголовке не меняет измеритель этой ячейки.'+hint]
    scope = proposal.scope_quote
    if (scope and (clean(scope) not in clean(source['text'])
            or not quoted(proposal.value, proposal.quote, scope))):
        return [prefix+'scope_quote должна быть буквальным фрагментом того же источника, содержащим quote и число.'+hint]
    if proposal.scope_state == 'verified' and not scope:
        return [prefix+'для заявленной проверенной области отсутствует буквальная scope_quote.'+hint]
    return []
