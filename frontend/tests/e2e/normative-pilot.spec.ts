import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('finds cable norms in the real local corpus and retains an engineer selection', async ({
  page,
}) => {
  test.setTimeout(90000)
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires the isolated fixture and read-only corpus')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'table-run.json'), 'utf8'),
  )
  await page.setViewportSize({ width: 1600, height: 1200 })
  await page.goto(`/audits/${run}?position=0`)
  const title = page.locator('.normative-panel > summary')
  await expect(title).toBeVisible({ timeout: 25000 })
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true)
  const panel = page.getByRole('region', { name: 'Нормативный подбор', exact: true })
  await panel.locator('.normative-tools > summary').click()
  const query = panel.getByRole('textbox', { name: 'Работа или код нормы', exact: true })
  await expect(query).toBeVisible({ timeout: 15000 })
  await query.fill('08-02-148-02')
  await panel.locator('.normative-facts > summary').click()
  await panel
    .getByLabel('Способ прокладки', { exact: true })
    .selectOption('laid_pipes_blocks_boxes')
  await panel.getByLabel('Напряжение, кВ', { exact: true }).fill('10')
  await panel.getByLabel('Масса кабеля, кг/м', { exact: true }).fill('1,8')
  await panel.getByLabel('Количество ВОР', { exact: true }).fill('250')
  await panel.getByLabel('Единица ВОР', { exact: true }).fill('м')
  await panel.getByLabel('Высота монтажа, м', { exact: true }).fill('12')
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith('/normative/search')),
    panel.getByRole('button', { name: 'Найти нормативные варианты', exact: true }).click(),
  ])
  const exact = panel
    .locator('article')
    .filter({ has: page.getByRole('checkbox', { name: 'Выбрать 08-02-148-02', exact: true }) })
  await expect(exact).toBeVisible({ timeout: 25000 })
  await expect(exact).toContainText('2,5 измерителя нормы')
  for (const checkbox of await panel.locator('article input[type="checkbox"]').all()) {
    await checkbox.uncheck()
  }
  await exact.getByRole('checkbox', { name: 'Выбрать 08-02-148-02', exact: true }).check()
  await panel.locator('article input[type="checkbox"]:not(:checked)').first().check()
  const selectionReply = page.waitForResponse((response) =>
    response.url().endsWith('/normative/selection'),
  )
  await panel.getByRole('button', { name: 'Сохранить выбор инженера · 2', exact: true }).click()
  const selection = await selectionReply
  expect(selection.request().postDataJSON().norm_ids).toHaveLength(2)
  expect((await selection.json()).payload.selected_ids).toHaveLength(2)
  await expect(panel.getByRole('status')).toContainText('Выбор инженера сохранён', {
    timeout: 15000,
  })
  const citation = exact.locator('a[href*="/sources/"]').first()
  const href = (await citation.getAttribute('href'))!
  const response = await page.request.get(href.split('#')[0]!, {
    headers: { Range: 'bytes=0-1023' },
  })
  expect(response.status()).toBe(206)
  expect((await response.body()).subarray(0, 5).toString()).toBe('%PDF-')
  await page.reload()
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true)
  await expect(panel.locator('.normative-saved')).toContainText('Выбор инженера · 2', {
    timeout: 25000,
  })
  await expect(panel.locator('.normative-saved')).toContainText('08-02-148-02')
  await panel
    .locator('.normative-saved')
    .screenshot({ path: '../.audit_cache/normative-selection.png' })
  await page.screenshot({ path: '../.audit_cache/normative-pilot.png', fullPage: true })
})
