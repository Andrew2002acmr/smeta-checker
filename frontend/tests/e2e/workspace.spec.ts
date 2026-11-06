import { test, expect } from './auth'
import type { Page } from '@playwright/test'
import { resolve } from 'node:path'
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'

async function dropFiles(page: Page, files: { name: string; mimeType: string; buffer: Buffer }[]) {
  const payload = files.map((file) => ({
    name: file.name,
    mimeType: file.mimeType,
    base64: file.buffer.toString('base64'),
  }))
  await page.getByRole('region', { name: 'Перетащите папку или файлы' }).evaluate((zone, items) => {
    const transfer = new DataTransfer()
    for (const item of items) {
      const bytes = Uint8Array.from(atob(item.base64), (char) => char.charCodeAt(0))
      transfer.items.add(new File([bytes], item.name, { type: item.mimeType }))
    }
    zone.dispatchEvent(
      new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true }),
    )
  }, payload)
}

async function createProject(page: Page, name: string) {
  await page.goto('/upload')
  await page.getByLabel('Название проекта', { exact: true }).fill(name)
  await page.getByRole('button', { name: 'Создать проект', exact: true }).click()
  await expect(page).toHaveURL(/\/projects\/[^/?]+$/)
  await page.locator('a[href*="/departments/"]').filter({ hasText: 'Служба изысканий' }).click()
  await expect(page).toHaveURL(/\/projects\/[^/?]+\/departments\/[^/?]+$/)
  await expect(page.getByRole('region', { name: 'Перетащите папку или файлы' })).toBeVisible()
}
test('history stays loading until audits arrive and handles an empty project list', async ({
  page,
}) => {
  let release!: () => void
  const gate = new Promise<void>((resolveGate) => {
    release = resolveGate
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

test('dashboard filters projects and opens the permanent project workspace', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an explicitly isolated fixture')
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Проекты' })).toBeVisible()
  await expect(page.getByRole('link', { name: /Синтетический стенд · интеграция/ })).toBeVisible({
    timeout: 30000,
  })
  await page.getByLabel('Поиск проектов').fill('стенд')
  await expect(page.getByRole('list', { name: 'Список проектов' })).toContainText(
    'Синтетический стенд · интеграция',
  )
  await page.getByLabel('Поиск проектов').fill('нет-такого-проекта')
  await expect(page.getByText('ничего не найдено', { exact: false })).toBeVisible()
  await page.getByLabel('Поиск проектов').fill('')
  await page.getByRole('link', { name: 'Открыть проект Синтетический стенд · интеграция' }).click()
  await expect(page).toHaveURL(/\/projects\/[^/?]+$/)
  await expect(
    page.getByRole('heading', { name: 'Синтетический стенд · интеграция', exact: true }),
  ).toBeVisible()
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
  await page.getByRole('button', { name: /^Готовые позиции: \d+$/ }).click()
  await expect(page.getByRole('button', { name: 'Открыть позицию', exact: true })).toBeVisible()
  await expect(page.getByRole('progressbar')).toBeHidden()
})

test('BFF keeps the token on the server and enforces its session', async ({ request, baseURL }) => {
  expect((await request.get('/api/v1/projects')).status()).toBe(401)
  expect(
    (
      await request.post('/api/session', { headers: { Origin: 'https://unrelated.invalid' } })
    ).status(),
  ).toBe(403)
  const session = await request.post('/api/session', {
    headers: { Origin: baseURL! },
    data: { login: process.env.WORKSPACE_LOGIN, password: process.env.WORKSPACE_PASSWORD },
  })
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
  await page.getByRole('button', { name: /^Готовые позиции: \d+$/ }).click()
  await page.getByRole('button', { name: 'Открыть позицию', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Сверка ВОР с PDF' })).toBeVisible()
  await expect(page.locator('.pdf-stage canvas')).toHaveAttribute('width', /[1-9]\d*/)
  const dialog = page.getByRole('dialog', { name: 'Сверка ВОР с PDF' })
  await expect
    .poll(() => dialog.evaluate((element) => element.contains(document.activeElement)))
    .toBe(true)
  await page.keyboard.press('Shift+Tab')
  await expect
    .poll(() => dialog.evaluate((element) => element.contains(document.activeElement)))
    .toBe(true)
  await page.getByRole('button', { name: 'Оглавление', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Дерево разделов PDF' })).toBeVisible()
  await expect(page.locator('.sheet-scroll')).toContainText('Copper cable')
  // The position card shows parsed work fields rather than spreadsheet column letters.
  await expect(page.locator('.excel-pane')).toContainText('Наименование')
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('button', { name: 'Открыть позицию', exact: true })).toBeFocused()
  await page.getByRole('button', { name: 'Открыть позицию', exact: true }).click()
  await expect(dialog).toBeVisible()
  await page.reload()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.locator('.pdf-stage canvas')).toHaveAttribute('width', /[1-9]\d*/)
  await page.getByRole('button', { name: 'Закрыть сверку' }).click()
  await expect(dialog).toBeHidden()
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Скачать Word', exact: true }).click()
  expect((await download).suggestedFilename()).toMatch(/\.docx$/)
  await page.screenshot({ path: '../.audit_cache/e2e-results.jpg', fullPage: true })
})

test('unsupported files are explained before upload', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Writes require an explicitly isolated fixture')
  await createProject(page, `Неподдерживаемый файл ${Date.now()}`)
  await dropFiles(page, [
    {
      name: 'unsupported.exe',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from('test'),
    },
  ])
  await expect(page.getByRole('alert')).toContainText('XLSX/CSV')
  await expect(
    page.getByRole('button', { name: 'Сохранить документы', exact: true }),
  ).toBeDisabled()
})

test('upload, preflight, audit, persisted results and Word export use real API', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Writes require an explicitly isolated fixture')
  const name = `Синтетическая загрузка · E2E ${Date.now()}`
  await createProject(page, name)
  const vor = [
    '№ п.п.;Наименование работ, ресурсов, затрат по проекту;Объём работ / Количество;Ед. изм.;Ссылка на ПД;Наименование файла;Номер страниц (через пробел)',
    '1;Copper cable;45;m;;project.pdf;1',
    '2;Insulated wire;12;m;;other.pdf;1',
  ].join('\n')
  await page.getByLabel('Добавить файлы', { exact: true }).setInputFiles([
    { name: 'vor.csv', mimeType: 'text/csv', buffer: Buffer.from('\uFEFF' + vor) },
    {
      name: 'project.pdf',
      mimeType: 'application/pdf',
      buffer: readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'project.pdf')),
    },
  ])
  const importDialog = page.getByRole('dialog')
  await page.getByRole('button', { name: 'Сохранить документы', exact: true }).click()
  await expect(importDialog).toBeHidden()
  await page.locator('a[href*="/documents/"]').filter({ hasText: 'vor.csv' }).first().click()
  await page.getByRole('button', { name: 'Проверить', exact: true }).click()
  await page.getByLabel('Подтверждаю запуск несмотря на критические ошибки').check()
  await page.getByRole('button', { name: 'Проверить', exact: true }).click()
  await expect(page).toHaveURL(/\/audits\/[^/?]+$/)
  await page.getByRole('button', { name: /^Готовые позиции: \d+$/ }).click()
  await expect(
    page.getByRole('button', { name: 'Открыть позицию', exact: true }).first(),
  ).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: /^Готовые позиции: \d+$/ }).click()
  await page.getByRole('button', { name: 'Открыть позицию', exact: true }).first().click()
  const review = page.getByRole('dialog', { name: 'Сверка ВОР с PDF' })
  await expect(review).toContainText('Позиция 1 из 2')
  await review.getByRole('button', { name: 'Следующая позиция' }).click()
  await expect(review).toContainText('Позиция 2 из 2')
  await expect(review.getByRole('button', { name: 'Следующая позиция' })).toBeDisabled()
  await review.getByRole('button', { name: 'Предыдущая позиция' }).click()
  await expect(review).toContainText('Позиция 1 из 2')
  await review.getByRole('button', { name: 'Закрыть сверку' }).click()
  await expect(review).toBeHidden()
  const downloading = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Скачать Word', exact: true }).click()
  expect((await downloading).suggestedFilename()).toMatch(/\.docx$/)
})

test('folder picker includes nested documents without a typed path', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires the isolated document fixture')
  const folder = mkdtempSync(resolve(tmpdir(), 'smeta-folder-picker-'))
  try {
    mkdirSync(resolve(folder, 'project'))
    copyFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'vor.xlsx'), resolve(folder, 'vor.xlsx'))
    copyFileSync(
      resolve(process.env.E2E_FIXTURE_ROOT!, 'project.pdf'),
      resolve(folder, 'project', 'project.pdf'),
    )
    await createProject(page, `Комплект из папки ${Date.now()}`)
    await page.getByLabel('Добавить папку', { exact: true }).setInputFiles(folder)
    await expect(page.getByRole('list', { name: 'Файлы к сохранению' })).toContainText('vor.xlsx')
    await expect(page.getByRole('list', { name: 'Файлы к сохранению' })).toContainText(
      'project.pdf',
    )
    await page.getByRole('button', { name: 'Сохранить документы', exact: true }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await expect(
      page.locator('a[href*="/documents/"]').filter({ hasText: 'vor.xlsx' }),
    ).toBeVisible()
  } finally {
    rmSync(folder, { recursive: true, force: true })
  }
})

test('mobile review switches between full-width sources', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires the isolated document fixture')
  const runId = readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'run-id.txt'), 'utf8').trim()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/audits/' + runId + '?position=0')
  const dialog = page.getByRole('dialog', { name: 'Сверка ВОР с PDF' })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.excel-pane')).toContainText('Copper cable')
  await expect(dialog.locator('.aw-pdf')).toBeHidden()
  await dialog.getByRole('button', { name: 'PDF', exact: true }).click()
  await expect(dialog.locator('.excel-pane')).toBeHidden()
  await expect(dialog.locator('.pdf-stage canvas')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'PDF', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await dialog.getByRole('button', { name: 'Данные ВОР', exact: true }).click()
  await expect(dialog.locator('.excel-pane')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Предыдущая позиция' })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: 'Следующая позиция' })).toBeDisabled()
})
