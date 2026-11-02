import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('context question and grounded explanations are readable without paid model calls', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires isolated corpus fixture')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'table-run.json'), 'utf8'),
  )
  await page.route(`**/api/v1/audits/${run}/positions/0/normative/search`, async (route) => {
    const body = route.request().postDataJSON()
    expect(body.mode).toBe('context')
    expect(body.query).toBe('Линия до щита: как учесть прокладку кабеля?')
    // API tests cover real context persistence and the fake provider. Here the
    // real corpus response drives the UI, with a synthetic bounded explanation.
    const response = await route.fetch({
      postData: JSON.stringify({ ...body, mode: 'lexical', query: '08-02-148-02' }),
    })
    const data = await response.json()
    data.payload.mode = 'context'
    data.payload.query = body.query
    data.payload.context_analysis = {
      interpretation: 'Прокладка кабельной линии до щита; способ прокладки нужно подтвердить.',
      queries: ['кабель в трубах', '08-02-148-02'],
      missing_facts: ['Способ прокладки'],
      warnings: [],
      model: 'synthetic-context',
      provider: 'test',
      requests: 2,
      elapsed_seconds: 1.2,
      usage: { input: 250, output: 50, total: 300 },
    }
    data.payload.candidates = data.payload.candidates.slice(0, 1)
    data.payload.candidates[0].context_reason =
      'Предложено по составу работ: прокладка кабеля в трубах. Сверьте условия и оригинал на странице 85.'
    await route.fulfill({ response, json: data })
  })
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.goto(`/audits/${run}?position=0`)
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true, { timeout: 25000 })
  const panel = page.getByRole('region', { name: 'Нормативный подбор', exact: true })
  await panel.locator('.normative-tools > summary').click()
  await panel
    .getByLabel('Работа или код нормы', { exact: true })
    .fill('Линия до щита: как учесть прокладку кабеля?')
  await panel.getByRole('button', { name: 'Подобрать по контексту', exact: true }).click()
  const analysis = panel.getByRole('region', { name: 'Контекст нормативного поиска', exact: true })
  await expect(analysis).toContainText('Прокладка кабельной линии до щита', { timeout: 25000 })
  await expect(analysis).toContainText('кабель в трубах')
  await expect(panel.locator('article')).toContainText('Почему предложено:')
  await expect(panel.locator('article a[href*="/sources/"]').first()).toBeVisible()
  const size = await panel.evaluate((el) => ({ client: el.clientWidth, scroll: el.scrollWidth }))
  expect(size.scroll).toBeLessThanOrEqual(size.client + 1)
  await analysis.screenshot({ path: '../.audit_cache/normative-context.png' })
})
