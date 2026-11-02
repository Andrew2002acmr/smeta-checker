import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('long audit rows and the single VOR row fit without horizontal scrolling', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires the isolated table fixture')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'table-run.json'), 'utf8'),
  )
  await page.route(`**/api/v1/audits/${run}/rows?*`, async (route) => {
    const response = await route.fetch()
    const body = await response.json()
    for (const row of body.rows)
      row.problem =
        'Объём 108,66 м³ в проекте не нашёлся. Дополнительные подробности доступны внутри позиции.'
    await route.fulfill({ response, json: body })
  })
  for (const width of [1600, 1280, 900, 600]) {
    await page.setViewportSize({ width, height: 1000 })
    await page.goto(`/audits/${run}`)
    await expect(page.getByRole('button', { name: 'Открыть позицию', exact: true })).toHaveCount(10)
    const sizes = await page.locator('.results').evaluate((el) => ({
      client: el.clientWidth,
      scroll: el.scrollWidth,
    }))
    expect(sizes.scroll, `audit table at ${width}px`).toBeLessThanOrEqual(sizes.client + 1)
    await expect(page.locator('.row-problem').first()).toHaveText(
      'Объём 108,66 м³ в проекте не нашёлся.',
    )
    if (width >= 900) {
      const headers = page.locator('.results thead th')
      await expect(headers).toHaveCount(4)
      await expect(headers.nth(1)).toHaveText('Статус')
      const name = await headers.nth(0).boundingBox()
      const values = await headers.nth(3).boundingBox()
      expect(name!.width).toBeGreaterThan(values!.width * 2)
      const firstRow = page.locator('.results tbody tr').first()
      await expect(firstRow.locator('td').nth(1)).toHaveText('Подтверждено')
      await expect(firstRow.locator('td').nth(2)).not.toContainText('Подтверждено')
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width,
    )
  }
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.screenshot({ path: '../.audit_cache/audit-list-compact.png', fullPage: true })
  await page.getByRole('button', { name: 'Открыть позицию', exact: true }).first().click()
  const vor = page.getByRole('region', { name: 'Таблица ВОР', exact: true })
  await expect(vor.locator('tbody tr')).toHaveCount(1, { timeout: 25000 })
  await expect(vor.getByRole('columnheader', { name: 'J', exact: true })).toHaveCount(0)
  await expect(vor.getByRole('columnheader', { name: 'K', exact: true })).toHaveCount(0)
  await expect(vor.getByText('INTERNAL_ID')).toHaveCount(0)
  await expect(vor.getByText('Примечание к выбранной позиции:', { exact: false })).toBeHidden()
  await vor.locator('summary').filter({ hasText: 'Примечание' }).click()
  await expect(vor.getByText('Примечание к выбранной позиции:', { exact: false })).toBeVisible()
  const sizes = await vor
    .locator('.sheet-scroll')
    .evaluate((el) => ({ client: el.clientWidth, scroll: el.scrollWidth }))
  expect(sizes.scroll).toBeLessThanOrEqual(sizes.client + 1)
  await page.screenshot({ path: '../.audit_cache/table-layout.png', fullPage: true })
})
