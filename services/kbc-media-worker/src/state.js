import fs from 'node:fs/promises';
import path from 'node:path';
import { config } from './config.js';

const statePath = path.join(config.dataDir, 'heartbeat.json');

let current = { status: 'starting', job_id: null };

export async function heartbeat(extra = {}) {
  current = { ...current, ...extra };
  await fs.mkdir(config.dataDir, { recursive: true });
  await fs.writeFile(statePath, JSON.stringify({
    ts: new Date().toISOString(),
    pid: process.pid,
    worker_id: config.workerId,
    ...current
  }), 'utf8');
}

// Long jobs (a 3-hour FFmpeg run or a slow SharePoint upload) must keep the
// heartbeat fresh, otherwise the Docker healthcheck marks the container
// unhealthy while it is doing exactly what it should.
export function startHeartbeatTimer() {
  const timer = setInterval(() => {
    heartbeat().catch(() => {});
  }, config.heartbeatIntervalSeconds * 1000);
  timer.unref();
  return () => clearInterval(timer);
}

export { statePath };
