import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test.beforeEach(async ({ page }) => {
  if (!process.env.E2E_FIXTURE_ROOT) return
  const runId = readFileSync(resolve(process.env.E2E_FIXTURE_ROOT, 'run-id.txt'), 'utf8').trim()
  const url = `/api/v1/audits/${runId}/positions/0/basis`
  const response = await page.request.get(url)
  expect(response.ok()).toBeTruthy()
  for (const item of await response.json()) {
    const removed = await page.request.delete(`${url}/${item.id}`, {
      headers: { Origin: process.env.FRONTEND_URL || 'http://127.0.0.1:3000' },
    })
    expect(removed.ok()).toBeTruthy()
  }
})

test('user confirms, selects PDF evidence, reloads and revokes a choice', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an isolated synthetic workspace')
  const runId = readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'run-id.txt'), 'utf8').trim()
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.goto(`/audits/${runId}?position=0`)
  await page.getByRole('button', { name: 'Подтвердить основание', exact: true }).first().click()
  const reviewed = page.getByRole('region', { name: 'Выбор пользователя' })
  await expect(reviewed).toContainText('basis-e2e')
  await expect(reviewed.getByText('Подтверждено пользователем', { exact: true })).toHaveCount(1)
  await page.reload()
  await expect(reviewed.getByText('Подтверждено пользователем', { exact: true })).toHaveCount(1)
  await page.getByRole('button', { name: 'Указать основание', exact: true }).click()
  await page.getByRole('button', { name: 'Область', exact: true }).click()
  await expect(page.locator('.pdf-draw')).toBeVisible()
  await page.locator('.pdf-scroll').evaluate((element) => {
    element.scrollTop = 0
  })
  const stage = await page.locator('.pdf-stage').boundingBox()
  if (!stage) throw new Error('PDF stage is unavailable')
  await page.mouse.move(stage.x + stage.width * 0.12, stage.y + stage.height * 0.08)
  await page.mouse.down()
  await page.mouse.move(stage.x + stage.width * 0.75, stage.y + stage.height * 0.15, { steps: 8 })
  await page.mouse.up()
  await expect(page.getByRole('textbox', { name: 'Текст выбранного основания' })).toBeVisible()
  await page.getByRole('textbox', { name: 'Комментарий к основанию' }).fill('Ручное основание e2e')
  await page.getByRole('button', { name: 'Сохранить основание', exact: true }).click()
  await expect(reviewed.getByText('Ручное основание e2e', { exact: true })).toBeVisible()
  await page.reload()
  await expect(reviewed.getByText('Ручное основание e2e', { exact: true })).toBeVisible()
  await reviewed.getByRole('button', { name: 'Показать в PDF', exact: true }).last().click()
  await expect(page.locator('.pdf-mark.human').first()).toBeVisible()
  await page.screenshot({ path: '../.audit_cache/human-basis.png', fullPage: true })
  await reviewed
    .getByRole('button', { name: 'Отменить подтверждение', exact: true })
    .first()
    .click()
  await expect(reviewed.getByText('Подтверждено пользователем', { exact: true })).toHaveCount(1)
  await expect(
    page.getByRole('button', { name: 'Подтвердить основание', exact: true }).first(),
  ).toBeEnabled()
})

test('user can save selected PDF text as an evidence region', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an isolated synthetic workspace')
  const runId = readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'run-id.txt'), 'utf8').trim()
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.goto(`/audits/${runId}?position=0`)
  await page.getByRole('button', { name: 'Указать основание', exact: true }).click()
  await page.getByRole('button', { name: 'Текст', exact: true }).click()
  await expect(page.locator('.textLayer span').first()).toBeVisible()
  const textBox = await page.locator('.textLayer span').first().boundingBox()
  if (!textBox) throw new Error('PDF text is unavailable')
  await page.mouse.move(textBox.x + 2, textBox.y + textBox.height / 2)
  await page.mouse.down()
  await page.mouse.move(textBox.x + textBox.width * 0.8, textBox.y + textBox.height / 2, {
    steps: 12,
  })
  await page.mouse.up()
  await expect(page.getByRole('textbox', { name: 'Текст выбранного основания' })).toBeVisible()
  await page
    .getByRole('textbox', { name: 'Комментарий к основанию' })
    .fill('Текстовое основание e2e')
  await page.getByRole('button', { name: 'Сохранить основание', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Выбор пользователя' })).toContainText(
    'Текстовое основание e2e',
  )
})

test('one click selects exactly one table row with a matching preview and saved quote', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an isolated synthetic workspace')
  const created = await page.request.post('/api/v1/projects', {
    data: { name: 'Точность выделения e2e' },
    headers: { Origin: process.env.FRONTEND_URL! },
  })
  const project = await created.json()
  const csv =
    '№ п.п.;Наименование работ;Объём работ / Количество;Ед. изм.;Наименование файла;Номера страниц\n1;Copper cable;45;m;table.pdf;1\n'
  const vor = await page.request.post('/api/v1/documents', {
    multipart: {
      project_id: project.id,
      kind: 'vor',
      new_document: 'true',
      file: { name: 'selection.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
    headers: { Origin: process.env.FRONTEND_URL! },
  })
  const pdf = await page.request.post('/api/v1/documents', {
    multipart: {
      project_id: project.id,
      kind: 'pdf',
      new_document: 'true',
      file: {
        name: 'table.pdf',
        mimeType: 'application/pdf',
        buffer: readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'selection-table.pdf')),
      },
    },
    headers: { Origin: process.env.FRONTEND_URL! },
  })
  expect(vor.ok()).toBeTruthy()
  expect(pdf.ok()).toBeTruthy()
  const started = await page.request.post('/api/v1/audits', {
    data: {
      project_id: project.id,
      vor_version_id: (await vor.json()).version_id,
      pdf_version_ids: [(await pdf.json()).version_id],
    },
    headers: { Origin: process.env.FRONTEND_URL! },
  })
  expect(started.ok()).toBeTruthy()
  const runId = (await started.json()).run_id
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/v1/audits/${runId}/progress`)).json()).status,
      { timeout: 45000 },
    )
    .toBe('done')
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.goto(`/audits/${runId}?position=0`)
  await page.getByRole('button', { name: 'Указать основание', exact: true }).click()
  const text = page
    .locator('.textLayer span')
    .filter({ hasText: /^Copper cable$/ })
    .first()
  await expect(text).toBeVisible()
  const target = await text.boundingBox()
  if (!target) throw new Error('Table row not visible')
  await page.mouse.click(target.x + target.width / 2, target.y + target.height / 2)
  const preview = page.getByRole('textbox', { name: 'Текст выбранного основания' })
  await expect(preview).toHaveValue(/Copper cable/)
  await expect(preview).not.toHaveValue(/NEXT ROW|999/)
  const shown = await preview.inputValue()
  await page.screenshot({ path: '../.audit_cache/precise-row-selection.png', fullPage: true })
  await page.getByRole('button', { name: 'Сохранить основание', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Выбор пользователя' })).toBeVisible()
  const saved = await (await page.request.get(`/api/v1/audits/${runId}/positions/0/basis`)).json()
  expect(saved[0].quote).toBe(shown)
  expect(saved[0].region.highlights).toHaveLength(3)
})
