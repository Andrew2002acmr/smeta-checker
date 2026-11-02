import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('evidence text has readable paragraphs and Russian labels without changing its source', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires the isolated table fixture')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'table-run.json'), 'utf8'),
  )
  const quote =
    '**006/26-ТКР1**\n\n## **13 Обоснование требований к грунтам отсыпки**\n\nДля отсыпки **используется грунт от разработки выемки**.\n\n' +
    'Влажность и гранулометрический состав проверяются по проекту. '.repeat(12) +
    '\n\nКоличество: 114/23,53 шт./т.'
  let savedIndex: number | undefined
  await page.route(`**/api/v1/audits/${run}/positions/0/workspace`, async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    body.evidence = [{ ...body.evidence[0], quote, kind: 'technology' }]
    await route.fulfill({ response, json: body })
  })
  await page.route(`**/api/v1/audits/${run}/positions/0/basis/confirm`, async (route) => {
    savedIndex = route.request().postDataJSON().evidence_index
    await route.fulfill({ status: 409, json: { error: { message: 'Тест: сохранение отключено' } } })
  })
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.goto(`/audits/${run}?position=0`)
  const evidence = page.locator('.aw-evidence-list')
  await expect(evidence).toContainText('Способ выполнения', { timeout: 25000 })
  await expect(evidence).not.toContainText('technology')
  await expect(evidence.locator('blockquote')).not.toContainText('**')
  await expect(evidence.locator('blockquote')).not.toContainText('##')
  await evidence.getByRole('button', { name: 'Показать полностью', exact: true }).click()
  await expect(evidence).toContainText('114/23,53 шт./т.')
  const style = await evidence
    .locator('blockquote')
    .evaluate((el) => ({ whiteSpace: getComputedStyle(el).whiteSpace, text: el.textContent }))
  expect(style.whiteSpace).toBe('pre-line')
  expect(style.text).toContain('\n\n')
  await page.screenshot({ path: '../.audit_cache/evidence-text.png', fullPage: true })
  // Evidence is confirmed by index; display formatting must not replace its source.
  await evidence.getByRole('button', { name: 'Подтвердить основание', exact: true }).click()
  await expect.poll(() => savedIndex).toBe(0)
})
