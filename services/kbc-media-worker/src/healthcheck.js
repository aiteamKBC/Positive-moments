import fs from 'node:fs/promises';
import { statePath } from './state.js';
import { config } from './config.js';

const maxAgeMs = Math.max(config.heartbeatIntervalSeconds * 4, 120) * 1000;

try {
  const state = JSON.parse(await fs.readFile(statePath, 'utf8'));
  const ageMs = Date.now() - Date.parse(state.ts);
  if (!Number.isFinite(ageMs) || ageMs > maxAgeMs) process.exit(1);
  process.exit(0);
} catch {
  process.exit(1);
}
