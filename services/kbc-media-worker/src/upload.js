import fs from 'node:fs';
import fsp from 'node:fs/promises';
import https from 'node:https';
import { config } from './config.js';
import { sleep } from './http.js';
import { redactText } from './redact.js';

function parseResponseBody(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch { return { raw: redactText(text).slice(0, 2000) }; }
}

// Graph answers a 308 with the byte ranges it still wants. Trusting it instead
// of our own counter keeps a retried chunk from being written twice or skipped.
function nextExpectedStart(body) {
  const ranges = body?.nextExpectedRanges;
  if (!Array.isArray(ranges) || ranges.length === 0) return null;
  const start = Number(String(ranges[0]).split('-')[0]);
  return Number.isFinite(start) && start >= 0 ? start : null;
}

function putChunk(uploadUrl, filePath, start, endInclusive, totalSize) {
  return new Promise((resolve, reject) => {
    const url = new URL(uploadUrl);
    if (url.protocol !== 'https:') {
      reject(new Error('Upload session URL must be https'));
      return;
    }
    const contentLength = endInclusive - start + 1;

    const req = https.request(url, {
      method: 'PUT',
      timeout: config.requestTimeoutSeconds * 1000,
      headers: {
        'Content-Length': String(contentLength),
        'Content-Range': `bytes ${start}-${endInclusive}/${totalSize}`,
        'Content-Type': 'application/octet-stream'
      }
    }, res => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', chunk => { text += chunk; });
      res.on('end', () => {
        const body = parseResponseBody(text);
        const status = Number(res.statusCode || 0);
        if ((status >= 200 && status < 300) || status === 308) {
          resolve({ status, body, headers: res.headers });
          return;
        }
        // The message must never carry the upload URL back to n8n or the logs.
        const error = new Error(`Upload chunk failed with HTTP ${status}`);
        error.status = status;
        error.response = body;
        error.headers = res.headers;
        reject(error);
      });
    });

    req.on('timeout', () => req.destroy(new Error('Upload chunk request timed out')));
    req.on('error', reject);
    fs.createReadStream(filePath, { start, end: endInclusive })
      .on('error', reject)
      .pipe(req);
  });
}

export async function uploadFileBySession(uploadUrl, filePath, onProgress = () => {}) {
  const stat = await fsp.stat(filePath);
  const totalSize = stat.size;
  if (totalSize <= 0) throw new Error('Cannot upload an empty file');

  let start = 0;
  let finalDriveItem = null;

  while (start < totalSize) {
    const endInclusive = Math.min(start + config.uploadChunkBytes - 1, totalSize - 1);
    let result;

    for (let attempt = 1; attempt <= config.uploadMaxRetries; attempt++) {
      try {
        result = await putChunk(uploadUrl, filePath, start, endInclusive, totalSize);
        break;
      } catch (error) {
        const status = error.status;
        // 416 means Graph already holds this range; re-sync from its answer.
        const retryable = !status || status === 429 || status === 416 || status >= 500;
        if (!retryable || attempt >= config.uploadMaxRetries) throw error;
        const headerDelay = Number(error.headers?.['retry-after'] || 0);
        const delayMs = headerDelay > 0 ? headerDelay * 1000 : Math.min(1000 * (2 ** attempt), 30000);
        await sleep(delayMs);
      }
    }

    if (result.status === 200 || result.status === 201) {
      finalDriveItem = result.body;
      start = totalSize;
    } else {
      const expected = nextExpectedStart(result.body);
      const advanced = expected !== null ? expected : endInclusive + 1;
      if (advanced <= start) {
        throw new Error(`Upload stalled: Microsoft Graph still expects bytes from ${advanced}`);
      }
      start = advanced;
    }

    onProgress({ uploadedBytes: Math.min(start, totalSize), totalBytes: totalSize });
  }

  if (!finalDriveItem?.id) {
    throw new Error('Upload session finished but Microsoft Graph returned no final DriveItem ID');
  }
  return finalDriveItem;
}
