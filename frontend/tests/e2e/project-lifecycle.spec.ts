import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// Real BFF/API + isolated synthetic auditor: no provider calls or quota.
test('revisions retain document identity and historical evidence after PD changes', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Writes require the isolated fixture')
  test.setTimeout(90000)
  const name = `Редакции ВОР и ПД ${Date.now()}`
  const pdf = readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'project.pdf'))
  const csv =
    '№ п.п.;Наименование работ;Объём работ / Количество;Ед. изм.;Ссылка на ПД;Наименование файла;Номер страниц (через пробел)\n1;Copper cable;45;m;;project.pdf;1\n'
  await page.goto('/upload')
  await page.getByLabel('Название проекта', { exact: true }).fill(name)
  await page.getByTestId('create-project').click()
  await expect(page.getByRole('region', { name: 'Перетащите папку или файлы' })).toBeVisible()
  const projectId = new URL(page.url()).pathname.split('/')[2]!
  await page.getByLabel('Добавить файлы', { exact: true }).setInputFiles([
    { name: 'vor.csv', mimeType: 'text/csv', buffer: Buffer.from('\uFEFF' + csv) },
    { name: 'project.pdf', mimeType: 'application/pdf', buffer: pdf },
  ])
  await page.getByTestId('save-documents').click()
  await expect(page.getByRole('dialog')).toBeHidden()
  await page.locator('a[href*="/documents/"]').filter({ hasText: 'vor.csv' }).first().click()
  await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/documents/[^/?]+$`))
  const vorPath = new URL(page.url()).pathname
  const vorDocumentId = vorPath.split('/').at(-1)!
  await page.getByTestId('start-audit').click()
  await expect(page).toHaveURL(/\/audits\/[^/?]+$/)
  await expect(page.getByTestId('download-word')).toBeVisible()
  const firstRunId = new URL(page.url()).pathname.split('/')[2]!
  const beforeResponse = await page.request.get(`/api/v1/audits/${firstRunId}/summary`)
  expect(beforeResponse.status()).toBe(200)
  const before = await beforeResponse.json()
  const oldVorVersion = before.run.vor_version_id
  const oldPdVersion = before.documents.find(
    (document: { kind: string }) => document.kind === 'pdf',
  ).id

  await page.goto(vorPath)
  await page.getByLabel('Файл новой редакции ВОР', { exact: true }).setInputFiles({
    name: 'vor-corrected.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('\uFEFF' + csv.replace(';45;', ';46;')),
  })
  await expect(page.getByRole('list', { name: 'Все редакции ведомости' })).toContainText(
    'vor-corrected.csv, редакция 2',
  )
  const vorResponse = await page.request.get(`/api/v1/documents/${vorDocumentId}/detail`)
  const vor = await vorResponse.json()
  expect(vor.id).toBe(vorDocumentId)
  expect(vor.versions.map((version: { filename: string }) => version.filename).toSorted()).toEqual([
    'vor-corrected.csv',
    'vor.csv',
  ])
  expect(vor.current_version_id).not.toBe(oldVorVersion)

  const catalogResponse = await page.request.get(`/api/v1/projects/${projectId}/catalog`)
  const catalog = await catalogResponse.json()
  const pd = catalog.find((document: { kind: string }) => document.kind === 'pdf')
  await page.goto(`/projects/${projectId}/documents/${pd.id}`)
  await page.getByLabel('Файл новой редакции ПД', { exact: true }).setInputFiles({
    name: 'project-revision2.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.concat([pdf, Buffer.from('\n%revision 2\n')]),
  })
  await expect(page.getByRole('list', { name: 'Все редакции ПД' })).toContainText(
    'project-revision2.pdf, редакция 2',
  )
  const pendingPd = await (await page.request.get(`/api/v1/documents/${pd.id}/detail`)).json()
  expect(pendingPd.current_version_id).toBe(oldPdVersion)
  await page
    .getByRole('button', { name: 'Сделать действующей редакцию 2', exact: true })
    .first()
    .click()
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/v1/documents/${pd.id}/detail`)).json())
          .current_version_id,
    )
    .not.toBe(oldPdVersion)
  const updatedPd = await (await page.request.get(`/api/v1/documents/${pd.id}/detail`)).json()

  const after = await (await page.request.get(`/api/v1/audits/${firstRunId}/summary`)).json()
  expect(after.run.vor_version_id).toBe(oldVorVersion)
  expect(after.documents.map((document: { id: string }) => document.id).toSorted()).toEqual(
    before.documents.map((document: { id: string }) => document.id).toSorted(),
  )
  const historicalPdf = await page.request.get(`/api/v1/documents/${oldPdVersion}/content`)
  expect(await historicalPdf.body()).toEqual(pdf)

  await page.goto(vorPath)
  // Авто-пул берёт действующую редакцию ПД — выбирать нечего.
  await page.getByTestId('start-audit').click()
  await expect(page).toHaveURL(/\/audits\/[^/?]+$/)
  await expect(page.getByTestId('download-word')).toBeVisible()
  const secondRunId = new URL(page.url()).pathname.split('/')[2]!
  const second = await (await page.request.get(`/api/v1/audits/${secondRunId}/summary`)).json()
  expect(second.run.vor_version_id).toBe(vor.current_version_id)
  expect(second.documents.find((document: { kind: string }) => document.kind === 'pdf').id).toBe(
    updatedPd.current_version_id,
  )
  expect(secondRunId).not.toBe(firstRunId)

  const deletion = await page.request.delete(`/api/v1/documents/${oldVorVersion}/version`, {
    headers: { Origin: new URL(page.url()).origin },
  })
  expect(deletion.status()).toBe(409)
  await page.goto(`/audits/${firstRunId}?position=0`)
  await expect(page.getByRole('dialog', { name: 'Сверка ВОР с PDF' })).toBeVisible()
  await expect(page.locator('.pdf-stage canvas')).toHaveAttribute('width', /[1-9]\d*/)
  await expect(page.getByRole('region', { name: 'Таблица ВОР', exact: true })).toContainText(
    'Copper cable',
  )
})
