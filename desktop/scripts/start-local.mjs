// Source deployment: Electron owns the Python backend; this process owns Vite
// and the Electron process tree. No frozen backend or installer is involved.
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { spawn, spawnSync } from 'node:child_process'

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(import.meta.url)
const dev = process.argv.includes('--dev')
let server
let electron
let stopping = false

async function stop(code) {
  if (stopping) return
  stopping = true
  if (electron?.pid && electron.exitCode === null) {
    if (process.platform === 'win32') {
      spawnSync('taskkill.exe', ['/PID', String(electron.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
    } else electron.kill('SIGTERM')
  }
  await server?.close()
  process.exitCode = code
}

process.once('SIGINT', () => void stop(0))
process.once('SIGTERM', () => void stop(0))

try {
  if (dev) {
    const { createServer } = await import('vite')
    server = await createServer({
      configFile: path.join(desktop, 'vite.config.ts'),
      server: { host: 'localhost', port: 5173, strictPort: true },
    })
    await server.listen()
    console.log('React 热更新：http://localhost:5173；Python/主进程改动后重启此命令。')
  }
  const env = { ...process.env, COW_DESKTOP_RENDERER_MODE: dev ? 'dev' : 'built' }
  delete env.ELECTRON_RUN_AS_NODE
  electron = spawn(require('electron'), ['.', ...process.argv.slice(2).filter(arg => arg !== '--dev')], {
    cwd: desktop, stdio: 'inherit', env,
  })
  const code = await new Promise((resolve, reject) => {
    electron.once('error', reject)
    electron.once('exit', code => resolve(code ?? 1))
  })
  await stop(code)
} catch (error) {
  console.error('本地启动失败：', error.message)
  await stop(1)
}
