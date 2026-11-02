import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('binds a renamed PDF copy to a VOR reference and keeps the selection after reload', async ({
  page,
}) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires an isolated synthetic workspace')
  const origin = process.env.FRONTEND_URL!
  const headers = { Origin: origin }
  const projectResponse = await page.request.post('/api/v1/projects', {
    data: { name: 'Привязка копии ПД' },
    headers,
  })
  expect(projectResponse.ok()).toBeTruthy()
  const project = await projectResponse.json()
  const uploaded = []
  for (const [name, kind, mime, file] of [
    [
      'vor.xlsx',
      'vor',
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'vor.xlsx',
    ],
    ['project.pdf', 'pdf', 'application/pdf', 'project.pdf'],
    ['copy-for-review.pdf', 'pdf', 'application/pdf', 'project.pdf'],
  ]) {
    const response = await page.request.post('/api/v1/documents', {
      headers,
      multipart: {
        project_id: project.id,
        kind: kind!,
        new_document: 'true',
        file: {
          name: name!,
          mimeType: mime!,
          buffer: readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, file!)),
        },
      },
    })
    expect(response.ok()).toBeTruthy()
    uploaded.push(await response.json())
  }
  const vor = uploaded[0],
    copy = uploaded[2]
  await page.goto(`/projects/${project.id}/documents/${vor.document_id}`)
  const select = page.getByLabel('ПД для ссылки project.pdf', { exact: true })
  await expect(select).toBeVisible()
  await select.selectOption(copy.version_id)
  await expect(page.getByRole('button', { name: 'Проверить', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Сохранить привязки ПД', exact: true }).click()
  await expect(
    page.getByText('Привязки ПД сохранены для этой версии ВОР.', { exact: true }),
  ).toBeVisible()
  await page.reload()
  await expect(select).toHaveValue(copy.version_id)
  const preflight = await page.request.post('/api/v1/documents/preflight', {
    headers,
    data: { project_id: project.id, vor_version_id: vor.version_id },
  })
  expect(preflight.ok()).toBeTruthy()
  const result = await preflight.json()
  expect(result.has_critical_error).toBe(false)
  expect(result.rows[0].pdf).toContain('copy-for-review.pdf')
  await page.screenshot({ path: '../.audit_cache/pdf-bindings.png', fullPage: true })
})
