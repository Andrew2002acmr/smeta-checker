import { test, expect } from './auth'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

test('continues a stopped audit without losing the ready batch', async ({ page }) => {
  test.skip(!process.env.E2E_FIXTURE_ROOT, 'Requires the isolated progressive fixture')
  const { run_id: run } = JSON.parse(
    readFileSync(resolve(process.env.E2E_FIXTURE_ROOT!, 'progressive-run.json'), 'utf8'),
  )
  await page.setViewportSize({ width: 1600, height: 1100 })
  await page.goto(`/audits/${run}`)
  const ready = page.getByRole('button', { name: 'Открыть позицию', exact: true })
  const resume = page.getByRole('button', { name: 'Продолжить обработку', exact: true })
  await expect(ready).toHaveCount(10)
  await page.getByRole('button', { name: 'Остановить проверку', exact: true }).click()
  await expect(resume).toBeVisible()
  await expect(ready).toHaveCount(10)
  await page.screenshot({ path: '../.audit_cache/resume-audit.png', fullPage: true })
  await resume.click()
  await expect(page.getByRole('button', { name: 'Остановить проверку', exact: true })).toBeVisible()
  await expect(resume).toHaveCount(0)
  await expect(ready).toHaveCount(10)
  await page.reload()
  await expect(page.getByRole('button', { name: 'Остановить проверку', exact: true })).toBeVisible()
  await expect(ready).toHaveCount(10)
})
