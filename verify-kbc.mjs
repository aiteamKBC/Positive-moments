import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'

const root = process.cwd()
const envText = await readFile(path.join(root, 'backend', '.env'), 'utf8')
const env = Object.fromEntries(envText.split(/\r?\n/).map((line) => line.match(/^([^#=]+)=(.*)$/)).filter(Boolean).map((match) => [match[1].trim(), match[2].trim().replace(/^['"]|['"]$/g, '')]))
const auth = await fetch('http://127.0.0.1:8000/api/auth/login/', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ username: env.DASHBOARD_USERNAME, password: env.DASHBOARD_PASSWORD }) })
if (!auth.ok) throw new Error(`Authentication failed: ${auth.status}`)
const { token } = await auth.json()

const output = path.join(root, 'output', 'kbc-ui-review')
await mkdir(output, { recursive: true })
const chrome = spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', ['--headless=new', '--remote-debugging-port=9229', `--user-data-dir=${path.join(output, 'chrome-profile')}`, '--no-first-run', '--no-default-browser-check', 'about:blank'], { stdio: 'ignore' })

async function retry(fn, attempts = 40) { let last; for (let i = 0; i < attempts; i += 1) { try { return await fn() } catch (error) { last = error; await new Promise((resolve) => setTimeout(resolve, 250)) } } throw last }
await retry(async () => { const response = await fetch('http://127.0.0.1:9229/json/version'); if (!response.ok) throw new Error('Chrome not ready') })
const target = await (await fetch('http://127.0.0.1:9229/json/new?http://localhost:5173/login', { method: 'PUT' })).json()
const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject })
let id = 0
const pending = new Map()
const browserErrors = []
socket.onmessage = (event) => {
  const message = JSON.parse(event.data)
  if (message.method === 'Runtime.exceptionThrown') browserErrors.push(message.params.exceptionDetails.text)
  if (message.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(message.params.type)) browserErrors.push(message.params.args.map((arg) => arg.value ?? arg.description).join(' '))
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id) }
}
function send(method, params = {}) { const messageId = ++id; socket.send(JSON.stringify({ id: messageId, method, params })); return new Promise((resolve) => pending.set(messageId, resolve)) }
await send('Page.enable'); await send('Runtime.enable')
await send('Page.navigate', { url: 'http://localhost:5173/login' })
await new Promise((resolve) => setTimeout(resolve, 1000))
await send('Runtime.evaluate', { expression: `localStorage.setItem('positive_mentions_token', ${JSON.stringify(token)})` })

const screens = [
  ['operations-1440', '/operations?date=2026-09-18', 1440, 1200],
  ['operations-1920', '/operations?date=2026-09-18', 1920, 1200],
  ['operations-1366', '/operations?date=2026-09-18', 1366, 1000],
  ['operations-empty-day', '/operations?date=2026-09-20', 1440, 900],
  ['lectures-1440', '/lectures', 1440, 1100],
  ['lectures-1366', '/lectures', 1366, 1000],
  ['positive-moments-1440', '/positive-moments', 1440, 1100],
  ['positive-moments-tablet', '/positive-moments', 768, 1100],
  ['positive-moments-mobile', '/positive-moments', 390, 844],
  ['moment-detail', '/positive-moments/a3RWaXpJREdBQUFBZ3ZCeGxBVFpNREU1T2pNeU0yWm1OVEZoTVdNMU5qUm1abVE0T0Rsak9HRTNaVFF6TTJVNE5ERmhRSFJvY21WaFpDNTBZV04yTXEweE56Z3dNems1TnpFd016UXcyVHcwT1daa1pUY3pPUzFoTmpka0xUUmtaVFV0T1daa1pTMWlOekE0WXpCa05qWTRNVGt0TVRjNE5EZzRPVGszTWkxVWNtRnVjMk55YVhCMFZqST0', 1440, 1200],
  ['lecture-detail', '/lectures/ea3e1c87-5c6c-5394-82b1-ff9fb8307ab6', 1440, 1200],
  ['lecture-detail-pipeline', '/lectures/ea3e1c87-5c6c-5394-82b1-ff9fb8307ab6?tab=pipeline', 1440, 1400],
  ['lecture-detail-qa', '/lectures/5c84a940-ca3a-5901-9be4-c6db4d199ac4?tab=qa', 1440, 900],
  ['lecture-detail-suppressed', '/lectures/26e74d25-ea3f-5e3d-ab08-19ab949eb75f', 1440, 900],
  ['pending-attendance', '/operations/pending-attendance?date=2026-09-18', 1440, 900],
  ['manual-review', '/operations/manual-review?date=2026-09-18', 1440, 800],
  ['pipeline-runs', '/operations/runs', 1440, 1000],
  ['operations-mobile', '/operations?date=2026-09-18', 390, 844],
]
const report = []
for (const [name, route, width, height] of screens) {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 600 })
  await send('Page.navigate', { url: `http://localhost:5173${route}` })
  const slow = route.startsWith('/operations?') || route.includes('pending-attendance')
    || route.includes('manual-review') || /\/lectures(\?|$)/.test(route) || /\/lectures\//.test(route)
  const settle = slow ? 30000 : 6000
  await new Promise((resolve) => setTimeout(resolve, settle))
  const state = await send('Runtime.evaluate', { expression: `({ title: document.title, heading: document.querySelector('h1')?.textContent?.trim(), text: document.body.innerText.slice(0, 3000), overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth })`, returnByValue: true })
  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await writeFile(path.join(output, `${name}-${width}.png`), Buffer.from(shot.result.data, 'base64'))
  report.push({ name, route, width, ...state.result.result.value })
}
await send('Runtime.evaluate', { expression: `localStorage.removeItem('positive_mentions_token')` })
for (const [name, width, height, mobile] of [['login-desktop', 1440, 950, false], ['login-mobile', 390, 844, true]]) {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile })
  await send('Page.navigate', { url: 'http://localhost:5173/login' })
  await new Promise((resolve) => setTimeout(resolve, 1500))
  const loginShot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await writeFile(path.join(output, `${name}-${width}.png`), Buffer.from(loginShot.result.data, 'base64'))
}
await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
const resources = await send('Runtime.evaluate', { expression: `performance.getEntriesByType('resource').map((entry) => entry.name)`, returnByValue: true })
await writeFile(path.join(output, 'audit.json'), JSON.stringify({ browserErrors, resourceHosts: [...new Set(resources.result.result.value.map((value) => new URL(value).host))] }, null, 2))
socket.close(); chrome.kill()
console.log(JSON.stringify(report.map(({ name, title, heading, overflow, text }) => ({ name, title, heading, overflow, excerpt: text.slice(0, 220).replace(/\n/g, ' | ') })), null, 2))
console.log(JSON.stringify({ browserErrors, resourceHosts: [...new Set(resources.result.result.value.map((value) => new URL(value).host))] }, null, 2))
