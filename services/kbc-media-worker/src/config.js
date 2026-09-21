import path from 'node:path';
import { loadDotEnv, serviceRoot } from './env.js';

loadDotEnv();

const missing = [];

function required(name) {
  const value = String(process.env[name] || '').trim();
  if (!value) missing.push(name);
  return value;
}

function int(name, fallback, min = 1) {
  const raw = process.env[name];
  const value = raw === undefined || raw === '' ? fallback : Number(raw);
  if (!Number.isInteger(value) || value < min) {
    throw new Error(`${name} must be an integer >= ${min}`);
  }
  return value;
}

function httpsUrl(name) {
  const value = required(name);
  if (value && !value.startsWith('https://')) {
    throw new Error(`${name} must be an https:// URL`);
  }
  return value;
}

// Default to the in-container path, but stay overridable so the same code runs
// directly on Windows or on a Linux VPS without edits.
const dataDir = process.env.DATA_DIR || (process.platform === 'win32'
  ? path.join(serviceRoot, 'data')
  : '/app/data');

export const config = {
  claimUrl: httpsUrl('N8N_CLAIM_URL'),
  uploadSessionUrl: httpsUrl('N8N_UPLOAD_SESSION_URL'),
  completeUrl: httpsUrl('N8N_COMPLETE_URL'),
  failUrl: httpsUrl('N8N_FAIL_URL'),
  secret: required('KBC_WORKER_SECRET'),
  workerId: required('WORKER_ID'),
  dataDir,
  tmpDir: process.env.TMP_DIR || path.join(dataDir, 'tmp'),
  pollIntervalSeconds: int('POLL_INTERVAL_SECONDS', 60),
  idleBackoffSeconds: int('IDLE_BACKOFF_SECONDS', 60),
  requestTimeoutSeconds: int('REQUEST_TIMEOUT_SECONDS', 120),
  ffmpegTimeoutMinutes: int('FFMPEG_TIMEOUT_MINUTES', 180),
  heartbeatIntervalSeconds: int('HEARTBEAT_INTERVAL_SECONDS', 30),
  uploadChunkBytes: int('UPLOAD_CHUNK_BYTES', 10485760, 327680),
  uploadMaxRetries: int('UPLOAD_MAX_RETRIES', 6),
  preciseVideoCodec: process.env.PRECISE_VIDEO_CODEC || 'libx264',
  precisePreset: process.env.PRECISE_PRESET || 'veryfast',
  preciseCrf: String(process.env.PRECISE_CRF || '20'),
  preciseAudioCodec: process.env.PRECISE_AUDIO_CODEC || 'aac',
  preciseAudioBitrate: process.env.PRECISE_AUDIO_BITRATE || '160k'
};

if (config.uploadChunkBytes % 327680 !== 0) {
  throw new Error('UPLOAD_CHUNK_BYTES must be a multiple of 320 KiB (327680 bytes).');
}

export const missingEnv = missing;

export function assertConfigComplete() {
  if (missing.length) {
    throw new Error(`Missing required environment variable(s): ${missing.join(', ')}`);
  }
}
