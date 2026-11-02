"""Close only evidence-backed numeric blockers; conflicting scopes stay open."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from normative_parameters import build_cards, quoted, unit_scale_quoted


def position(index=0, volume='10', unit='м2', work='Устройство верхнего слоя основания, h=10 см'):
    return SimpleNamespace(index=index, number=str(index+1), volume=volume, unit=unit,
        work_name=work, formula='', calculation={}, comment='', position_source=SimpleNamespace(row=18+index))


def source(identity, text, **extra):
    return dict(id=identity, kind='project', origin='native', version_id='same-document-version',
        page=1, label='ПД', text=text, **extra)


def proposal(identity, value='10', scope='Верхний слой основания, h=10 см'):
    return dict(parameter='thickness', source_id=identity, value=value, unit='см', quote=f'h={value} см',
        scope_state='verified', scope_quote=scope)


def thickness(result):
    work = position()
    return next(card for card in build_cards(work, result, {0:work}, {}) if card['id'] == 'thickness')


class NumericEvidenceTests(unittest.TestCase):
    def test_project_thickness_does_not_compete_with_norm_base_thickness(self):
        text = 'Верхний слой основания, h=10 см'
        project = source('p', text)
        norm = source('n', 'Нормативная базовая толщина, h=4 см')
        norm['kind'] = 'normative'
        result = dict(pipeline_version=3, sources=[project,norm], parameter_proposals=[
            proposal('p'), proposal('n', '4', norm['text'])])
        card = thickness(result)
        self.assertEqual(card['state'], 'confirmed')
        self.assertEqual(card['source_ids'], ['p'])
        result['parameter_proposals'][0]['scope_quote'] = 'Нижний слой основания, h=10 см'
        project['text'] = result['parameter_proposals'][0]['scope_quote']
        self.assertEqual(thickness(result)['state'], 'needs_facts')

    def test_only_normative_thickness_cannot_confirm_project(self):
        norm = source('n', 'Верхний слой основания, h=10 см')
        norm['kind'] = 'normative'
        result = dict(pipeline_version=3, sources=[norm], parameter_proposals=[proposal('n')])
        self.assertEqual(thickness(result)['state'], 'needs_facts')

    def test_same_verified_scope_on_two_pages_corroborates_and_keeps_both_anchors(self):
        text = 'Верхний слой основания, h=10 см'
        first, second = source('a', text), source('b', text)
        second['page'] = 9
        result = dict(pipeline_version=3, sources=[first,second], parameter_proposals=[proposal('a'),proposal('b')])
        before = deepcopy(result)
        card = thickness(result)
        self.assertEqual(card['state'], 'confirmed')
        self.assertEqual(card['source_ids'], ['a','b'])
        self.assertEqual([a['source_id'] for a in card['source_anchors']], ['a','b'])
        self.assertEqual(result, before)

    def test_same_number_does_not_merge_conflicting_versions_scopes_or_unverified_ocr(self):
        text = 'Верхний слой основания, h=10 см'
        for change in ('version', 'scope', 'ocr', 'disputed', 'value'):
            with self.subTest(change=change):
                first, second = source('a', text), source('b', text)
                second['page'] = 9
                proposals = [proposal('a'),proposal('b')]
                result = dict(pipeline_version=3,sources=[first,second],parameter_proposals=proposals)
                if change == 'version':
                    second['version_id'] = 'other-version'
                elif change == 'scope':
                    second['text'] = 'Другой участок. '+text
                    proposals[1]['scope_quote'] = second['text']
                elif change == 'ocr':
                    second['origin'] = 'ocr'
                elif change == 'disputed':
                    result['items'] = [dict(state='needs_facts',claim_ids=['geometry:thickness'],project_source_ids=['b'])]
                else:
                    second['text'] = text.replace('10', '11')
                    proposals[1] = proposal('b','11',second['text'])
                self.assertEqual(thickness(result)['state'], 'needs_facts')

    def test_spaced_decimal_is_not_an_integer_anchor(self):
        text = 'Плодородный слой толщиной 0, 20 м'
        self.assertFalse(quoted('20',text,text))
        self.assertFalse(quoted('0',text,text))
        self.assertTrue(quoted('0.20',text,text))
        self.assertTrue(quoted('20','Толщина 20 см','Толщина 20 см'))

    def test_broken_verified_citation_is_not_silently_discarded_for_another_scope(self):
        work=position(work='Устройство газона, h=20 см')
        first=source('a','Откос: толщина 20 см')
        second=source('b','Газон: толщина 0, 20 м')
        proposals=[dict(parameter='thickness',source_id=s['id'],value='20',unit='см',quote=s['text'],
            scope_state='verified',scope_quote=s['text']) for s in (first,second)]
        cards=build_cards(work,dict(pipeline_version=3,sources=[first,second],parameter_proposals=proposals),{0:work},{})
        self.assertEqual(next(c for c in cards if c['id']=='thickness')['state'],'needs_facts')

    def test_fabricated_quote_neither_confirms_alone_nor_vetoes_actual_source(self):
        text='Верхний слой основания, h=10 см'
        actual=proposal('p')
        fabricated=proposal('p','12','Верхний слой основания, h=12 см')
        result=dict(pipeline_version=3,sources=[source('p',text)],parameter_proposals=[fabricated])
        card=thickness(result)
        self.assertEqual(card['state'],'needs_facts')
        self.assertNotIn('found_value',card)
        result['parameter_proposals'].append(actual)
        card=thickness(result)
        self.assertEqual(card['state'],'confirmed')
        self.assertEqual(card['found_value'],'10')
        self.assertEqual(card['quote'],'h=10 см')

    def test_unit_measure_can_omit_one_without_fabricating_quote(self):
        for unit in ('т', 'м²', 'м3', 'шт.', 'кг'):
            with self.subTest(unit=unit):
                quote = f'Измеритель: {unit}'
                self.assertTrue(unit_scale_quoted('1',unit,quote,quote))
                self.assertFalse(unit_scale_quoted('10',unit,quote,quote))
                self.assertFalse(unit_scale_quoted('1',unit,quote,'Другая цитата'))
        self.assertFalse(unit_scale_quoted('1','м2','Измеритель: т','Измеритель: т'))
        self.assertFalse(unit_scale_quoted('1','т','Расход: т','Расход: т'))
        self.assertTrue(unit_scale_quoted('1000','м2','Измеритель: 1000 м2','Измеритель: 1000 м2'))

    def test_implicit_measure_card_requires_readable_norm_and_compatible_unit(self):
        work = position(unit='т',work='Перевозка материала')
        norm = source('n','Измеритель: т')
        norm['kind'] = 'normative'
        result = dict(sources=[norm],parameter_proposals=[dict(parameter='unit_scale',source_id='n',
            value='1',unit='т',quote=norm['text'])])
        def card():
            return next(c for c in build_cards(work,result,{0:work},{}) if c['role']=='unit_scale')
        self.assertEqual(card()['state'],'confirmed')
        self.assertEqual(card()['quote'],'Измеритель: т')
        norm['origin']='ocr_unreviewed'
        self.assertEqual(card()['state'],'needs_facts')
        norm['numeric_verified']=True
        self.assertEqual(card()['state'],'confirmed')
        work.unit='м2'
        self.assertEqual(card()['state'],'needs_facts')


class FormulaControlTests(unittest.TestCase):
    def test_round_precision_is_not_unknown_engineering_value(self):
        work = position(volume='1.235',unit='м3',work='Объём')
        work.formula='ROUND(D1,3)'
        work.calculation=dict(expression=work.formula,dependencies=[dict(cell='D1',value='1.23456')])
        cards=build_cards(work,dict(sources=[]),{0:work},{})
        precision=next(c for c in cards if c['id']=='factor:0:3')
        self.assertEqual(precision['state'],'confirmed')
        self.assertIn('ROUND',precision['reason'])
        # The same number also used as a multiplier has an unresolved role.
        work.formula='ROUND(D1*3,3)'
        work.volume='3.704'
        work.calculation['expression']=work.formula
        precision=next(c for c in build_cards(work,dict(sources=[]),{0:work},{}) if c['id']=='factor:0:3')
        self.assertEqual(precision['state'],'needs_facts')

    def test_direct_parent_kg_to_t_conversion_is_proved_but_untyped_divisor_is_not(self):
        parent=position(volume='500',unit='кг',work='Работа')
        child=position(index=1,volume='0.5',unit='т',work='Материал')
        child.formula='D18/1000'
        child.calculation=dict(expression=child.formula,dependencies=[dict(cell='D18',value='500')])
        def divisor(relation):
            return next(c for c in build_cards(child,dict(sources=[]),{0:parent,1:child},relation)
                if c['id']=='factor:1:1000')
        relation={'parent':{'index':0}}
        self.assertEqual(divisor(relation)['state'],'confirmed')
        self.assertIn('кг → т',divisor(relation)['reason'])
        self.assertEqual(divisor({})['state'],'needs_facts')
        parent.unit='м2'
        self.assertEqual(divisor(relation)['state'],'needs_facts')
        parent.unit='кг'
        child.formula='D18*0.8/1000'
        child.calculation['expression']=child.formula
        child.volume='0.4'
        self.assertEqual(divisor(relation)['state'],'needs_facts')


if __name__ == '__main__':
    unittest.main()
