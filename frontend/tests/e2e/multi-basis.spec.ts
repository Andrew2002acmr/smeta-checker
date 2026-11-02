import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('image-only PDF: multiple regions across pages, real crop OCR, corrections and reload', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an isolated workspace with crop OCR')
  const headers = { Origin: process.env.FRONTEND_URL! }
  const created = await page.request.post('/api/v1/projects', {
    headers,
    data: { name: 'Скан · составное основание' },
  })
  expect(created.ok()).toBeTruthy()
  const project = await created.json()
  const uploads = []
  for (const [name, kind, mime] of [
    ['vor.xlsx', 'vor', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    ['project.pdf', 'pdf', 'application/pdf'],
  ]) {
    const file = kind === 'pdf' ? 'multi-scan.pdf' : name!
    const response = await page.request.post('/api/v1/documents', {
      headers,
      multipart: {
        project_id: project.id,
        kind: kind!,
        new_document: 'true',
        file: {
          name: name!,
          mimeType: mime!,
          buffer: readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, file)),
        },
      },
    })
    expect(response.ok()).toBeTruthy()
    uploads.push(await response.json())
  }
  const started = await page.request.post('/api/v1/audits', {
    headers,
    data: {
      project_id: project.id,
      vor_version_id: uploads[0].version_id,
      pdf_version_ids: [uploads[1].version_id],
    },
  })
  expect(started.ok()).toBeTruthy()
  const run = (await started.json()).run_id
  await expect
    .poll(
      async () => (await (await page.request.get(`/api/v1/audits/${run}/progress`)).json()).status,
      { timeout: 60000 },
    )
    .toBe('done')
  await page.setViewportSize({ width: 1600, height: 1200 })
  await page.goto(`/audits/${run}?position=0`)
  await page.getByRole('button', { name: 'Указать основание', exact: true }).click()
  await page.getByRole('button', { name: 'Несколько областей', exact: true }).click()
  async function draw(rect: number[]) {
    await page.locator('.pdf-scroll').evaluate((el) => {
      el.scrollTop = 0
    })
    const canvas = page.locator('.pdf-stage canvas')
    await expect(canvas).toBeVisible()
    const box = await canvas.boundingBox()
    if (!box) throw new Error('PDF unavailable')
    await page.mouse.move(box.x + box.width * rect[0]!, box.y + box.height * rect[1]!)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width * rect[2]!, box.y + box.height * rect[3]!, {
      steps: 10,
    })
    await page.mouse.up()
    await expect(page.getByText('Уточняем выделение…', { exact: true })).toHaveCount(0)
  }
  await draw([0.07, 0.08, 0.56, 0.2])
  await draw([0.68, 0.08, 0.93, 0.2])
  await expect(page.getByRole('textbox', { name: 'Текст области 2', exact: true })).toBeVisible()
  const pageInput = page.getByRole('spinbutton', { name: 'Физическая страница PDF' })
  await pageInput.fill('2')
  await pageInput.press('Tab')
  await expect(pageInput).toHaveValue('2')
  await draw([0.07, 0.08, 0.56, 0.2])
  await page.getByRole('button', { name: 'Распознать выбранное', exact: true }).click()
  const first = page.getByRole('textbox', { name: 'Текст области 1', exact: true })
  await expect(first).toHaveValue(/COPPER/, { timeout: 90000 })
  await expect(page.getByRole('textbox', { name: 'Текст области 2', exact: true })).toHaveValue(
    /222[.,]78/,
  )
  await expect(page.getByRole('textbox', { name: 'Текст области 3', exact: true })).toHaveValue(
    /UNIT/,
  )
  await first.fill('COPPER CABLE corrected')
  await page.getByRole('checkbox', { name: 'Текст проверен по изображению' }).first().check()
  await page.getByRole('button', { name: 'Сохранить основание', exact: true }).click()
  const reviewed = page.getByRole('region', { name: 'Выбор пользователя' })
  await expect(reviewed).toContainText('COPPER CABLE corrected')
  await page.reload()
  await expect(reviewed).toContainText('COPPER CABLE corrected')
  await reviewed.getByRole('button', { name: 'Область 3 · стр. 2', exact: true }).click()
  await expect(page.getByRole('spinbutton', { name: 'Физическая страница PDF' })).toHaveValue('2')
  await expect(page.locator('.pdf-mark.human').first()).toBeVisible()
  const saved = await (await page.request.get(`/api/v1/audits/${run}/positions/0/basis`)).json()
  expect(saved[0].fragments).toHaveLength(3)
  expect(saved[0].fragments.map((f: { page: number }) => f.page)).toEqual([1, 1, 2])
  expect(saved[0].fragments[0].ocr_text).toContain('COPPER')
  expect(saved[0].fragments[0].text_checked).toBe(true)
  await page.screenshot({ path: '../.audit_cache/multi-basis.png', fullPage: true })
})
