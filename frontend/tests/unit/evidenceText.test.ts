import { describe, expect, it } from 'vitest'
import { evidenceText, evidenceExcerpt } from '../../app/utils/evidenceText'

describe('readable evidence without changing source quantities', () => {
  it('removes heading and bold markers while preserving paragraphs and arithmetic', () => {
    expect(
      evidenceText(
        '## **13 Обоснование требований**\n\nДля отсыпки **используется грунт**.\n2**3 + 4**2; 114/23,53 шт./т',
      ),
    ).toBe(
      '13 Обоснование требований\n\nДля отсыпки используется грунт.\n2**3 + 4**2; 114/23,53 шт./т',
    )
  })
  it('separates table cells and list entries without injecting HTML', () => {
    expect(evidenceText('- _<u>ИГЭ-1б — грунт щебенистый серый</u>_')).toBe(
      '- ИГЭ-1б — грунт щебенистый серый',
    )
    expect(evidenceText('A_B_C; 2 * 3 * 4')).toBe('A_B_C; 2 * 3 * 4')
    expect(
      evidenceText('<table><tr><td>Шпала</td><td>шт./т</td><td>114/23,53</td></tr></table>'),
    ).toBe('Шпала | шт./т | 114/23,53')
    expect(
      evidenceText('<p>Грунт&nbsp;&amp; песок</p><ul><li>Первый</li><li>Второй</li></ul>'),
    ).toBe('Грунт & песок\n\n• Первый\n• Второй')
  })
  it('cuts a preview at a word boundary and leaves the full quote available', () => {
    const original = '**Обоснование**\n' + 'Требования к материалу. '.repeat(40)
    expect(evidenceExcerpt(original).length).toBeLessThanOrEqual(421)
    expect(evidenceExcerpt(original)).toMatch(/материалу\.…$|к…$|Требования…$/)
    expect(evidenceText(original)).toContain('Требования к материалу. '.repeat(30))
    expect(original).toContain('**Обоснование**')
  })
})
