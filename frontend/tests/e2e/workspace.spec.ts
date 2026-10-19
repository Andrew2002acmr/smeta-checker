import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import { readFileSync } from 'node:fs'

test('history stays loading until audits arrive and handles an empty project list', async ({
  page,
}) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/projects', (route) =>
    route.fulfill({ json: [{ id: 'test-project', name: 'Test' }] }),
  )
  await page.route('**/api/v1/projects/test-project/audits?*', async (route) => {
    await gate
    await route.fulfill({ json: [] })
  })
  await page.goto('/history?project=')
  await expect(page.getByText('Открываем историю…')).toBeVisible()
  release()
  await expect(page.getByText('Открываем историю…')).toBeHidden()
  await expect(page.getByText('Сохранённых проверок пока нет.', { exact: false })).toBeVisible()
  await page.route('**/api/v1/projects', (route) => route.fulfill({ json: [] }))
  await page.reload()
  await expect(page.getByText('Сохранённых проверок пока нет.', { exact: false })).toBeVisible()
  await expect(page.getByText('Открываем историю…')).toBeHidden()
})

test('progress shows PDF preparation, survives a network error and reaches saved results', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an explicitly isolated fixture')
  const runId = readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'run-id.txt'), 'utf8').trim()
  let poll = 0
  await page.route(`**/api/v1/audits/${runId}/progress`, async (route) => {
    poll++
    if (poll === 2) {
      await route.fulfill({ status: 503, json: { error: { message: 'Temporary outage' } } })
      return
    }
    const data = await (await route.fetch()).json()
    if (poll < 4)
      Object.assign(data, {
        status: 'running',
        stage: poll === 1 ? 'preparing' : 'auditing',
        total: 2,
        done: poll === 1 ? 0 : 1,
        failed: 0,
        running: 1,
        queued: 0,
        preparation_done: 151,
        preparation_total: 303,
      })
    await route.fulfill({ json: data })
  })
  await page.goto('/audits/' + runId)
  await expect(page.getByRole('heading', { name: 'Подготавливаем документы' })).toBeVisible()
  await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '49')
  await expect(page.getByText('Страниц текущего PDF: 151 из 303.')).toBeVisible()
  await expect(page.getByRole('alert')).toContainText('Повторяем подключение', { timeout: 10000 })
  await expect(page.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50', {
    timeout: 10000,
  })
  await expect(page.locator('table.results tbody tr')).toHaveCount(1, { timeout: 10000 })
  await expect(page.getByRole('progressbar')).toBeHidden()
})

test('BFF keeps the token on the server and enforces its session', async ({ request, baseURL }) => {
  expect((await request.get('/api/v1/projects')).status()).toBe(401)
  expect(
    (
      await request.post('/api/session', { headers: { Origin: 'https://unrelated.invalid' } })
    ).status(),
  ).toBe(403)
  const session = await request.post('/api/session', { headers: { Origin: baseURL! } })
  expect(session.ok()).toBe(true)
  expect(session.headers()['set-cookie']).toContain('HttpOnly')
  const response = await request.get('/api/v1/projects')
  expect(response.ok()).toBe(true)
  expect(JSON.stringify(await response.json())).not.toContain('synthetic-e2e-only')
  expect(
    (
      await request.post('/api/v1/projects', {
        data: { name: 'forbidden' },
        headers: { Origin: 'https://unrelated.invalid' },
      })
    ).status(),
  ).toBe(403)
  expect((await request.get('/api/unknown')).status()).toBe(404)
})

test('saved audit opens exact input, actual PDF and survives reload', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an explicitly isolated fixture')
  const runId = readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'run-id.txt'), 'utf8').trim()
  await page.goto('/audits/' + runId)
  await expect(
    page.getByRole('heading', { name: 'Синтетический стенд · интеграция' }),
  ).toBeVisible()
  await expect(page.locator('table.results tbody tr')).toHaveCount(1)
  await page.getByRole('button', { name: 'Открыть →', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Сверка ВОР с PDF' })).toBeVisible()
  await expect(page.locator('.pdf-stage canvas')).toHaveAttribute('width', /[1-9]\d*/)
  await expect(page.getByRole('region', { name: 'Дерево разделов PDF' })).toBeVisible()
  await expect(page.locator('.sheet-scroll')).toContainText('Copper cable')
  await expect(page.locator('.excel-pane')).toContainText('C2')
  await page.reload()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.locator('.pdf-stage canvas')).toHaveAttribute('width', /[1-9]\d*/)
  await page.getByRole('button', { name: 'Закрыть ×' }).click()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Скачать Word', exact: true }).click()
  expect((await download).suggestedFilename()).toMatch(/\.docx$/)
  await page.screenshot({ path: '../.audit_cache/e2e-results.jpg', fullPage: true })
})

test('unsupported files are explained before upload', async ({ page }) => {
  await page.goto('/upload')
  await page.getByLabel('Добавить файлы').setInputFiles({
    name: 'unsupported.exe',
    mimeType: 'application/octet-stream',
    buffer: Buffer.from('test'),
  })
  await expect(page.getByRole('alert')).toContainText('XLSX/CSV')
  await expect(page.getByRole('button', { name: 'Сохранить документы' })).toBeDisabled()
})

test('upload, preflight, audit, persisted results and Excel export use real API', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Writes require an explicitly isolated fixture')
  await page.goto('/upload')
  await page.getByLabel('Название комплекта', { exact: true }).fill('Синтетическая загрузка · E2E')
  await page
    .getByLabel('Добавить файлы')
    .setInputFiles([
      resolve(process.env.E2E_FIXTURE_ROOT!, 'vor.xlsx'),
      resolve(process.env.E2E_FIXTURE_ROOT!, 'project.pdf'),
    ])
  await page.getByRole('button', { name: 'Сохранить документы', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Версии для проверки' })).toBeVisible({
    timeout: 30000,
  })
  await page.getByRole('button', { name: 'Проверить привязки файлов' }).click()
  await expect(page.getByRole('cell', { name: '🟢 Имя совпадает', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Запустить проверку', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Синтетическая загрузка · E2E' })).toBeVisible({
    timeout: 45000,
  })
  await expect(page.locator('table.results tbody tr')).toHaveCount(1)
  await page.reload()
  await expect(page.locator('table.results tbody tr')).toHaveCount(1)
  const downloading = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Скачать Excel', exact: true }).click()
  expect((await downloading).suggestedFilename()).toMatch(/\.xlsx$/)
})
