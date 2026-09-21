import { config } from './config.js';
import { redactUrl } from './redact.js';

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export async function postJson(url, body, { timeoutSeconds = config.requestTimeoutSeconds } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSeconds * 1000);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-KBC-Worker-Secret': config.secret
      },
      body: JSON.stringify(body),
      signal: controller.signal
    });

    const text = await response.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch { data = { raw: text }; }
    }

    if (!response.ok) {
      const error = new Error(`HTTP ${response.status} from ${redactUrl(url)}`);
      error.status = response.status;
      error.response = data;
      throw error;
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

export async function withRetry(fn, {
  attempts = 5,
  baseDelayMs = 1500,
  retryable = error => !error?.status || error.status === 429 || error.status >= 500
} = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await fn(attempt);
    } catch (error) {
      lastError = error;
      if (attempt >= attempts || !retryable(error)) throw error;
      const retryAfter = Number(error?.response?.retry_after_seconds || 0);
      const delay = retryAfter > 0 ? retryAfter * 1000 : baseDelayMs * (2 ** (attempt - 1));
      await sleep(Math.min(delay, 30000));
    }
  }
  throw lastError;
}

export { sleep };
