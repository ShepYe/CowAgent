import { app } from 'electron'
import fs from 'fs'
import path from 'path'
import os from 'os'

const configPath = app.isPackaged
  ? path.join(process.resourcesPath, 'app-config.json')
  : path.resolve(__dirname, '../../resources/app-config.json')
export const personalInstance = (() => {
  try { return JSON.parse(fs.readFileSync(configPath, 'utf8')).personalInstance === true }
  catch { return false }
})()
export const instanceDataDir = personalInstance
  ? path.resolve(process.env.COW_PERSONAL_DATA_DIR || path.join(os.homedir(), '.cow-personal'))
  : path.join(os.homedir(), '.cow')

export function initializePersonalInstance(): void {
  if (!personalInstance) return
  fs.mkdirSync(instanceDataDir, { recursive: true })
  const config = path.join(instanceDataDir, 'config.json')
  // Exclusive creation: an existing personal configuration is never replaced.
  if (!fs.existsSync(config)) {
    fs.writeFileSync(config, JSON.stringify({
      agent_workspace: path.join(instanceDataDir, 'workspace'),
      embedding_enabled: false,
      personal_instance: true,
      personal_assistant_enabled: true,
    }, null, 2) + '\n', { encoding: 'utf8', flag: 'wx' })
  }
  process.env.COW_HOME = instanceDataDir
}
