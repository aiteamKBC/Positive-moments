/**
 * Responsive check for the Historical Backfill page.
 *
 * Measures rather than asserts: at each width it reports whether the document
 * actually overflows horizontally, and captures a screenshot so the layout can
 * be looked at rather than taken on trust.
 *
 * Requires the dev servers: Django on 8000, Vite on 5173.
 */
import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'

const root = process.cwd()
const envText = await readFile(path.join(root, 'backend', '.env'), 'utf8')
const env = Object.fromEntries(
  envText.split(/\r?\n/).map((line) => line.match(/^([^#=]+)=(.*)$/)).filter(Boolean)
    .map((match) => [match[1].trim(), match[2].trim().replace(/^['"]|['"]$/g, '')]),
)

const auth = await fetch('http://127.0.0.1:8000/api/auth/login/', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ username: env.DASHBOARD_USERNAME, password: env.DASHBOARD_PASSWORD }),
})
if (!auth.ok) throw new Error(`Authentication failed: ${auth.status}`)
const { token } = await auth.json()

const output = path.join(root, 'output', 'kbc-backfill-review')
await mkdir(output, { recursive: true })

const chrome = spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', [
  '--headless=new', '--remote-debugging-port=9231',
  `--user-data-dir=${path.join(output, 'chrome-profile')}`,
  '--no-first-run', '--no-default-browser-check', 'about:blank',
], { stdio: 'ignore' })

async function retry(fn, attempts = 40) {
  let last
  for (let i = 0; i < attempts; i += 1) {
    try { return await fn() } catch (error) {
      last = error
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
  }
  throw last
}
await retry(async () => {
  const response = await fetch('http://127.0.0.1:9231/json/version')
  if (!response.ok) throw new Error('Chrome not ready')
})

const target = await (await fetch(
  'http://127.0.0.1:9231/json/new?http://localhost:5173/login', { method: 'PUT' },
)).json()
const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject })

let id = 0
const pending = new Map()
const browserErrors = []
socket.onmessage = (event) => {
  const message = JSON.parse(event.data)
  if (message.method === 'Runtime.exceptionThrown') {
    browserErrors.push(message.params.exceptionDetails.text)
  }
  if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
    browserErrors.push(message.params.args.map((a) => a.value ?? a.description).join(' '))
  }
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id) }
}
function send(method, params = {}) {
  const messageId = ++id
  socket.send(JSON.stringify({ id: messageId, method, params }))
  return new Promise((resolve) => pending.set(messageId, resolve))
}

await send('Page.enable')
await send('Runtime.enable')
await send('Page.navigate', { url: 'http://localhost:5173/login' })
await new Promise((resolve) => setTimeout(resolve, 1500))
await send('Runtime.evaluate', {
  expression: `localStorage.setItem('positive_mentions_token', ${JSON.stringify(token)})`,
})

const WIDTHS = [[1920, 1200], [1440, 1100], [1366, 1000], [768, 1100], [390, 844]]
const report = []

for (const [width, height] of WIDTHS) {
  await send('Emulation.setDeviceMetricsOverride', {
    width, height, deviceScaleFactor: 1, mobile: width < 600,
  })
  await send('Page.navigate', { url: 'http://localhost:5173/operations/backfill' })
  await new Promise((resolve) => setTimeout(resolve, 9000))

  const state = await send('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const doc = document.documentElement
      // Anything sticking out past the viewport, named so it can be fixed.
      const wide = [...document.querySelectorAll('body *')]
        .filter((el) => el.getBoundingClientRect().right > doc.clientWidth + 1)
        .slice(0, 5)
        .map((el) => el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : ''))
      return {
        heading: document.querySelector('h1')?.textContent?.trim(),
        overflow: doc.scrollWidth > doc.clientWidth,
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        offenders: wide,
        hasFromField: !!document.querySelector('input[type=date]'),
        buttons: [...document.querySelectorAll('button')].map((b) => b.textContent.trim()).filter(Boolean).slice(0, 6),
        text: document.body.innerText.slice(0, 400),
      }
    })()`,
  })

  const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await writeFile(path.join(output, `backfill-${width}.png`), Buffer.from(shot.result.data, 'base64'))
  report.push({ width, ...state.result.result.value })
}

console.log(JSON.stringify({ report, browserErrors }, null, 2))
await writeFile(path.join(output, 'report.json'),
  JSON.stringify({ report, browserErrors }, null, 2))
socket.close()
chrome.kill()
