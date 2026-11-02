import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('one position uses road norms and preserves batch results', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires isolated road-profile fixture API')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'table-run.json'), 'utf8'),
  )
  const base = `/api/v1/audits/${run}`
  const before = await (await page.request.get(`${base}/rows?offset=0&limit=200`)).json()
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.goto(`/audits/${run}?position=0`)
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true, { timeout: 25000 })
  const panel = page.getByRole('region', { name: 'Нормативный подбор', exact: true })
  await expect(panel).toContainText('ГЭСН27/47', { timeout: 15000 })
  const check = panel.getByRole('region', {
    name: 'Проверка одной позиции с нормативами',
    exact: true,
  })
  if (!(await check.getByLabel('Страницы ПД для проверки').isVisible()))
    await check.getByText('Перепроверить позицию', { exact: true }).click()
  await check.getByLabel('Страницы ПД для проверки').fill('1-8')
  await check.getByRole('button', { name: 'Проверить только эту позицию', exact: true }).click()
  await expect(check.getByRole('alert')).toContainText('шести')
  await check.getByLabel('Страницы ПД для проверки').fill('1')
  await check.getByRole('button', { name: 'Проверить только эту позицию', exact: true }).click()
  await expect(check).toContainText('Синтетическая точечная проверка', { timeout: 25000 })
  await check.locator('.decision-evidence > summary').click()
  await expect(check.getByRole('region', { name: 'Проверяемые параметры' })).toContainText(
    'Объём по проекту',
  )
  const citation = check.locator('a[href*="/sources/"]').first()
  const url = (await citation.getAttribute('href'))!.split('#')[0]!
  const response = await page.request.get(url, { headers: { Range: 'bytes=0-1023' } })
  expect(response.status()).toBe(206)
  expect((await response.body()).subarray(0, 5).toString()).toBe('%PDF-')
  const expectedPage = Number((await citation.getAttribute('href'))!.match(/#page=(\d+)/)![1])
  await citation.click()
  const viewer = page
    .getByRole('dialog')
    .filter({ has: page.getByRole('heading', { name: 'Источник документа', exact: true }) })
  await expect(viewer).toContainText(`стр. ${expectedPage}`)
  await expect(viewer.locator('canvas')).toBeVisible({ timeout: 30000 })
  await expect(viewer.locator('.pdf-anchor-mark').first()).toBeVisible({ timeout: 15000 })
  await expect(viewer.locator('.pdf-anchor-mark').first()).toBeInViewport()
  await viewer.screenshot({ path: '../.audit_cache/normative-source-anchor.png' })
  await viewer.getByRole('button', { name: 'Следующая страница PDF', exact: true }).click()
  await viewer.getByRole('button', { name: 'К основанию', exact: true }).click()
  await expect(viewer.locator('.pdf-anchor-mark').first()).toBeInViewport({ timeout: 15000 })
  await viewer.getByRole('button', { name: 'Вернуться к проверке' }).click()
  const after = await (await page.request.get(`${base}/rows?offset=0&limit=200`)).json()
  expect(
    after.rows.map((r: { audit_status: string; matching_report_status: string }) => [
      r.audit_status,
      r.matching_report_status,
    ]),
  ).toEqual(
    before.rows.map((r: { audit_status: string; matching_report_status: string }) => [
      r.audit_status,
      r.matching_report_status,
    ]),
  )
  await page.reload()
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true)
  await expect(check).toContainText('Синтетическая точечная проверка', { timeout: 25000 })
  const width = await check.evaluate((el) => ({ client: el.clientWidth, scroll: el.scrollWidth }))
  expect(width.scroll).toBeLessThanOrEqual(width.client + 1)
  await check.screenshot({ path: '../.audit_cache/normative-position-check.png' })
})

test('automatic stage refreshes without blocking the saved position', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires isolated fixture API')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'table-run.json'), 'utf8'),
  )
  const endpoint = `/api/v1/audits/${run}/positions/0/normative`
  let reads = 0
  await page.route(`**/api/v1/audits/${run}/positions/0/workspace`, async (route) => {
    const response = await route.fetch()
    const state = await response.json()
    if (reads < 2) {
      state.row.queue = 'pending'
      state.row.report_status = 'Проверяется по нормативам'
      state.row.problem = 'Сопоставление выполнено. Проверяем основание по нормативам.'
    }
    await route.fulfill({ response, json: state })
  })
  await page.route(`**${endpoint}`, async (route) => {
    const response = await route.fetch()
    const state = await response.json()
    state.automatic_stage = { status: ++reads === 1 ? 'running' : 'done', attempts: 1, message: '' }
    await route.fulfill({ response, json: state })
  })
  await page.goto(`/audits/${run}?position=0`)
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true, { timeout: 25000 })
  const panel = page.getByRole('region', { name: 'Нормативный подбор', exact: true })
  await expect(panel).toContainText('Нормативный разбор выполняется отдельно', { timeout: 15000 })
  const button = panel.getByRole('button', { name: 'Проверить только эту позицию', exact: true })
  await panel.getByText('Перепроверить позицию', { exact: true }).click()
  await expect(button).toBeDisabled()
  await expect(page.locator('.aw-verdict')).toContainText('Проверяется по нормативам')
  await expect(button).toBeEnabled({ timeout: 15000 })
  await expect(page.locator('.aw-verdict')).not.toContainText('Проверяется по нормативам', {
    timeout: 15000,
  })
  await expect(panel).toContainText('Синтетическая точечная проверка')
  await page.screenshot({ path: '../.audit_cache/normative-background.png', fullPage: true })
})

test('saved cell rectangles highlight a scan without a text layer and exclude the next row', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires isolated fixture API')
  const root = process.env.E2E_FIXTURE_ROOT!
  const { run_id: run } = JSON.parse(readFileSync(resolve(root, 'table-run.json'), 'utf8'))
  await page.route(`**/api/v1/audits/${run}/positions/0/normative`, async (route) => {
    const response = await route.fetch()
    const state = await response.json()
    const result = state.latest_check.payload.position_check
    const source = result.sources.find((s: { kind: string }) => s.kind === 'project')
    source.page = 1
    source.origin = 'ocr_unverified'
    const norm = result.sources.find((s: { label: string }) => s.label.endsWith('1.27.18'))
    const quote = norm.text.slice(norm.text.indexOf('При толщине нижнего')).split('.')[0]
    result.parameter_cards = [
      {
        id: 'quantity',
        label: 'Количество по ПД',
        role: 'project_value',
        declared_value: '222.78',
        unit: 'т',
        state: 'confirmed',
        reason: 'Синтетические точные координаты двух ячеек.',
        source_ids: [source.id],
        source_anchors: [
          {
            source_id: source.id,
            quote: 'COPPER CABLE · 222.78',
            rects: [
              [0.08, 0.09, 0.37, 0.17],
              [0.695, 0.09, 0.815, 0.17],
            ],
          },
        ],
      },
      {
        id: 'thickness',
        label: 'Толщина',
        role: 'project_value',
        declared_value: '24',
        unit: 'см',
        state: 'confirmed',
        reason: 'Синтетическая привязка к реальному абзацу норматива.',
        source_ids: [norm.id],
        source_anchors: [{ source_id: norm.id, quote, rects: [] }],
      },
    ]
    await route.fulfill({ response, json: state })
  })
  await page.route('**/api/v1/documents/*/content', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/pdf',
      body: readFileSync(resolve(root, 'multi-scan.pdf')),
    })
  })
  await page.goto(`/audits/${run}?position=0`)
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true, { timeout: 25000 })
  const check = page.getByRole('region', {
    name: 'Проверка одной позиции с нормативами',
    exact: true,
  })
  await check.locator('.decision-evidence > summary').click()
  await check.locator('.parameter-chain .source-anchor').first().click()
  const viewer = page
    .getByRole('dialog')
    .filter({ has: page.getByRole('heading', { name: 'Источник документа', exact: true }) })
  await expect(viewer.locator('.pdf-anchor-mark')).toHaveCount(2)
  await expect(viewer).toContainText('Нужный фрагмент подсвечен')
  await expect(viewer.locator('.textLayer')).toHaveText('')
  for (const mark of await viewer.locator('.pdf-anchor-mark').all()) {
    await expect(mark).toBeInViewport()
    expect(await mark.evaluate((el) => parseFloat((el as HTMLElement).style.top))).toBe(9)
    expect(await mark.evaluate((el) => parseFloat((el as HTMLElement).style.height))).toBeLessThan(
      9,
    )
  }
  await viewer.getByRole('button', { name: 'Увеличить PDF', exact: true }).click()
  await expect(viewer.locator('.pdf-anchor-mark')).toHaveCount(2)
  await viewer.getByRole('button', { name: 'Следующая страница PDF', exact: true }).click()
  await expect(viewer.locator('.pdf-anchor-mark')).toHaveCount(0)
  await viewer.getByRole('button', { name: 'К основанию', exact: true }).click()
  await expect(viewer.locator('.pdf-anchor-mark')).toHaveCount(2)
  await viewer.getByRole('button', { name: 'По ширине', exact: true }).click()
  await expect(viewer.locator('.pdf-anchor-mark').last()).toBeInViewport()
  await viewer.screenshot({ path: '../.audit_cache/normative-scanned-cells.png' })
  await viewer.getByRole('button', { name: 'Вернуться к проверке' }).click()
  await check.locator('.parameter-chain a[href*="/sources/"]').first().click()
  await expect(viewer).toContainText('Пункт 1.27.18')
  await expect(viewer).toContainText('Нужный фрагмент подсвечен')
  await expect(viewer.locator('.pdf-anchor-mark').first()).toBeInViewport()
  await viewer.screenshot({ path: '../.audit_cache/normative-exact-paragraph.png' })
})

test('decision offers concrete actions while calculations stay collapsed', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires isolated fixture API')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'table-run.json'), 'utf8'),
  )
  await page.route(`**/api/v1/audits/${run}/positions/0/workspace`, async (route) => {
    const response = await route.fetch(),
      data = await response.json()
    data.row.queue = 'review'
    await route.fulfill({ response, json: data })
  })
  await page.route(`**/api/v1/audits/${run}/positions/0/normative`, async (route) => {
    const response = await route.fetch(),
      data = await response.json()
    data.latest_check.payload.position_check.parameter_cards = [
      {
        id: 'quantity',
        label: 'Количество',
        role: 'project_value',
        declared_value: '431.61',
        unit: 'м2',
        state: 'needs_facts',
        reason: 'Нет структурированного подтверждения',
        required: true,
      },
      {
        id: 'unit_scale:missing',
        label: 'Измеритель',
        role: 'unit_scale',
        declared_value: '431.61',
        unit: 'м2',
        state: 'needs_facts',
        reason: 'Нет источника',
        required: true,
      },
      {
        id: 'factor:1',
        label: 'Материал',
        role: 'coefficient',
        declared_value: '1.26',
        unit: '',
        state: 'needs_facts',
        reason: 'Нет источника',
        required: false,
      },
    ]
    await route.fulfill({ response, json: data })
  })
  await page.goto(`/audits/${run}?position=0`)
  await expect(page.locator('.normative-panel')).toHaveJSProperty('open', true, { timeout: 25000 })
  const panel = page.getByRole('region', { name: 'Нормативный подбор', exact: true })
  const decision = panel.getByRole('region', { name: 'Итог и действия', exact: true })
  await expect(decision).toContainText('Не хватает подтверждения')
  await expect(decision).toContainText('Ошибка пока не доказана')
  await expect(decision).toContainText('строку таблицы ПД')
  await expect(decision).not.toContainText('1,26')
  await expect(panel.locator('.parameter-chain')).not.toBeVisible()
  await decision.getByRole('button', { name: 'Найти норматив', exact: true }).click()
  await expect(panel.getByLabel('Работа или код нормы', { exact: true })).toBeFocused()
  await decision.getByRole('button', { name: 'Указать верное основание в ПД', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Отмена', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Отмена', exact: true }).click()
  await decision.screenshot({ path: '../.audit_cache/normative-decision.png' })
})

test('quantity sum is visible immediately and opens both operands or one cell', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires isolated fixture API')
  const root = process.env.E2E_FIXTURE_ROOT!
  const { run_id: run } = JSON.parse(readFileSync(resolve(root, 'table-run.json'), 'utf8'))
  await page.route(`**/api/v1/audits/${run}/positions/0/normative`, async (route) => {
    const response = await route.fetch(),
      state = await response.json()
    const result = state.latest_check.payload.position_check
    const source = result.sources.find((s: { kind: string }) => s.kind === 'project')
    source.page = 1
    result.quantity_composition = {
      state: 'confirmed',
      unit: 'м2',
      total: '100',
      rounded_total: '100.00',
      declared_value: '100.00',
      source_id: source.id,
      page: 1,
      terms: [
        { label: 'слева', value: '40', rect: [0.08, 0.18, 0.14, 0.27] },
        { label: 'справа', value: '60', rect: [0.695, 0.18, 0.76, 0.27] },
      ],
    }
    await route.fulfill({ response, json: state })
  })
  await page.route('**/api/v1/documents/*/content', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/pdf',
      body: readFileSync(resolve(root, 'quantity-composition-scan.pdf')),
    })
  })
  await page.goto(`/audits/${run}?position=0`)
  const sum = page.getByRole('region', { name: 'Расчёт количества по таблице ПД', exact: true })
  await expect(sum).toContainText('100 м²', { timeout: 25000 })
  await expect(sum).toContainText('100,00 м²')
  await expect(sum).toContainText('Сумма совпадает')
  await expect(page.locator('.parameter-chain')).not.toBeVisible()
  await sum.getByRole('button', { name: 'Показать оба числа в ПД · стр. 1', exact: true }).click()
  const viewer = page
    .getByRole('dialog')
    .filter({ has: page.getByRole('heading', { name: 'Источник документа', exact: true }) })
  await expect(viewer.locator('.pdf-anchor-mark')).toHaveCount(2)
  await expect(viewer.locator('.textLayer')).toHaveText('')
  await expect(viewer.locator('.pdf-anchor-mark').last()).toBeInViewport()
  await viewer.screenshot({ path: '../.audit_cache/normative-quantity-operands.png' })
  await viewer.getByRole('button', { name: 'Вернуться к проверке' }).click()
  await sum.getByRole('button', { name: 'Показать справа: 60 в ПД', exact: true }).click()
  await expect(viewer.locator('.pdf-anchor-mark')).toHaveCount(1)
  expect(
    await viewer
      .locator('.pdf-anchor-mark')
      .evaluate((el) => parseFloat((el as HTMLElement).style.left)),
  ).toBe(69.5)
})
