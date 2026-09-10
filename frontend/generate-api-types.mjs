import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const candidates = process.platform === 'win32'
  ? [resolve(root, '.venv', 'Scripts', 'python.exe'), 'python']
  : [resolve(root, '.venv', 'bin', 'python'), 'python3', 'python']
const python = candidates.find((item) => !item.includes('.venv') || existsSync(item))
const exported = spawnSync(python, [resolve(root, 'scripts', 'export_openapi.py'),
  '--output', resolve(import.meta.dirname, 'openapi.json')], { stdio: 'inherit' })
if (exported.status !== 0) process.exit(exported.status || 1)

const cli = resolve(import.meta.dirname, 'node_modules', 'openapi-typescript', 'bin', 'cli.js')
const generated = spawnSync(process.execPath, [cli, 'openapi.json',
  '-o', 'src/api/schema.d.ts'], { stdio: 'inherit', cwd: import.meta.dirname })
if (generated.error) throw generated.error
process.exit(generated.status || 0)
