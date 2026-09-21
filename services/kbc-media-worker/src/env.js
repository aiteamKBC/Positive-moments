// Minimal .env loader so the worker runs identically under Docker (env_file)
// and directly on Windows (npm start). No dependency, no overwrite of values
// already present in the real environment.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const serviceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export function loadDotEnv(file = path.join(serviceRoot, '.env')) {
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch {
    return false;
  }

  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq <= 0) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) process.env[key] = value;
  }
  return true;
}

export { serviceRoot };
