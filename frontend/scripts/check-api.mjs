import { spawnSync } from 'node:child_process'
import { readFileSync, readdirSync, mkdirSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { resolve, join, sep, dirname } from 'node:path'
const root = process.cwd(),
  temporary = resolve(root, '.api-check')
if (!temporary.startsWith(root + sep)) throw Error('Unsafe temporary directory')
const python =
  process.env.PYTHON ||
  (process.platform === 'win32' ? resolve(root, '../.venv/Scripts/python.exe') : 'python')
const check = spawnSync(python, ['-m', 'scripts.export_frontend_openapi', '--check'], {
  cwd: resolve(root, '..'),
  encoding: 'utf8',
})
if (check.status !== 0) {
  process.stderr.write(check.stderr || check.stdout)
  process.exit(1)
}
function walk(dir, prefix = '') {
  return readdirSync(dir, { withFileTypes: true })
    .flatMap((entry) =>
      entry.isDirectory()
        ? walk(join(dir, entry.name), prefix + entry.name + '/')
        : [prefix + entry.name],
    )
    .sort()
}
try {
  mkdirSync(temporary, { recursive: true })
  const require = createRequire(import.meta.url)
  const generator = join(dirname(require.resolve('@hey-api/openapi-ts/package.json')), 'bin/run.js')
  const generated = spawnSync(process.execPath, [generator, '-o', temporary], {
    cwd: root,
    encoding: 'utf8',
  })
  if (generated.status !== 0) throw Error(generated.stderr || generated.stdout)
  const expected = resolve(root, 'app/generated'),
    files = walk(expected)
  if (JSON.stringify(files) !== JSON.stringify(walk(temporary)))
    throw Error('Generated client file set differs')
  for (const file of files)
    if (!readFileSync(join(expected, file)).equals(readFileSync(join(temporary, file))))
      throw Error('Stale generated client: ' + file)
  process.stdout.write('OpenAPI and generated SDK/Zod are current\n')
} finally {
  rmSync(temporary, { recursive: true, force: true })
}
