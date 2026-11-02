import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { execFileSync } from 'node:child_process'

test('reviews a batch while processing continues, restores a draft, and publishes the next batch on demand', async ({
  page,
}) => {
  test.skip(
    !process.env.E2E_PYTHON || !process.env.E2E_FIXTURE_ROOT,
    'Requires the isolated progressive fixture',
  )
  const root = process.env.E2E_FIXTURE_ROOT!
  const { run_id: run } = JSON.parse(readFileSync(resolve(root, 'progressive-run.json'), 'utf8'))
  await page.setViewportSize({ width: 1600, height: 1200 })
  await page.goto(`/audits/${run}`)
  await expect(page.getByRole('button', { name: 'Открыть позицию', exact: true })).toHaveCount(10)
  await expect(page.getByText(/Готово к просмотру: 10 из 12/)).toBeVisible()
  await page.getByRole('button', { name: 'Открыть позицию', exact: true }).last().click()
  await expect(page.getByRole('button', { name: 'Следующая позиция', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Закрыть сверку', exact: true }).click()
  await page.getByRole('button', { name: 'Открыть позицию', exact: true }).first().click()
  await page.getByRole('button', { name: 'Указать основание', exact: true }).click()
  await page.getByRole('button', { name: 'Несколько областей', exact: true }).click()
  await expect(page.locator('.pdf-draw')).toBeVisible()
  await page.locator('.pdf-scroll').evaluate((el) => {
    el.scrollTop = 0
  })
  const box = await page.locator('.pdf-stage canvas').boundingBox()
  if (!box) throw new Error('PDF not visible')
  await page.mouse.move(box.x + box.width * 0.1, box.y + box.height * 0.08)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.15, { steps: 8 })
  await page.mouse.up()
  const text = page.getByRole('textbox', { name: 'Текст области 1', exact: true })
  await expect(text).toBeEnabled()
  await text.fill('Промежуточный исправленный текст')
  await page.getByRole('button', { name: 'Сохранить черновик', exact: true }).click()
  await expect(
    page.getByText('Черновик сохранён. Его можно открыть в истории.', { exact: true }),
  ).toBeVisible({ timeout: 20000 })
  await page.getByRole('button', { name: 'Отмена', exact: true }).click()
  await page.getByRole('button', { name: 'История, черновики и OCR', exact: true }).click()
  const history = page.getByRole('region', { name: 'История и OCR', exact: true })
  await expect(history).toContainText('Черновики и распознавания · 1', { timeout: 20000 })
  await history.locator('details').filter({ hasText: 'Черновик ·' }).locator('summary').click()
  await history.getByRole('button', { name: 'Продолжить с этого варианта', exact: true }).click()
  await expect(text).toHaveValue('Промежуточный исправленный текст')
  await page.getByRole('button', { name: 'Сохранить основание', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Выбор пользователя' })).toContainText(
    'Промежуточный исправленный текст',
  )
  execFileSync(
    process.env.E2E_PYTHON!,
    [resolve('../backend/tests/progressive_fixture.py'), 'advance', root],
    { env: process.env },
  )
  await page.getByRole('button', { name: 'Закрыть сверку', exact: true }).click()
  await expect(
    page.getByRole('button', { name: 'Показать новые результаты · 1', exact: true }),
  ).toBeVisible({ timeout: 20000 })
  await expect(page.getByRole('button', { name: 'Открыть позицию', exact: true })).toHaveCount(10)
  await page.getByRole('button', { name: 'Показать новые результаты · 1', exact: true }).click()
  await page.getByRole('button', { name: 'Page 2', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Открыть позицию', exact: true })).toHaveCount(1)
  await page.screenshot({ path: '../.audit_cache/progressive-review.png', fullPage: true })
})
