import fs from 'node:fs/promises';
import path from 'node:path';
import { config, assertConfigComplete } from './config.js';
import { log } from './log.js';
import { postJson, sleep, withRetry } from './http.js';
import { cutVideo, validateJob } from './ffmpeg.js';
import { uploadFileBySession } from './upload.js';
import { heartbeat, startHeartbeatTimer } from './state.js';
import { redactText } from './redact.js';

const WORKER_VERSION = '1.1.0';

let stopping = false;
process.on('SIGTERM', () => { stopping = true; });
process.on('SIGINT', () => { stopping = true; });

// Error text travels to n8n and into qa_media_jobs.error, so it is redacted
// before it leaves this process.
function publicError(error) {
  return redactText(String(error?.message || error || 'Unknown error')).slice(0, 12000);
}

async function claimJob() {
  return withRetry(() => postJson(config.claimUrl, {
    worker_id: config.workerId,
    capabilities: ['precise', 'fast_copy'],
    job_types: ['positive_clip', 'lecture_part'],
    worker_version: WORKER_VERSION
  }), { attempts: 4 });
}

async function createUploadSession(job, output) {
  const response = await withRetry(() => postJson(config.uploadSessionUrl, {
    worker_id: config.workerId,
    job_id: job.job_id,
    output_filename: output.outputFilename,
    output_size_bytes: output.outputSizeBytes
  }), { attempts: 4 });

  const uploadUrl = String(response?.upload_url || response?.uploadUrl || '').trim();
  if (!uploadUrl.startsWith('https://')) throw new Error('n8n returned no valid Microsoft Graph upload session URL');
  return uploadUrl;
}

async function completeJob(job, output, driveItem) {
  return withRetry(() => postJson(config.completeUrl, {
    worker_id: config.workerId,
    job_id: job.job_id,
    output_filename: driveItem.name || output.outputFilename,
    output_size_bytes: Number(driveItem.size || output.outputSizeBytes),
    output_item_id: driveItem.id,
    output_drive_id: driveItem.parentReference?.driveId || null,
    output_web_url: driveItem.webUrl || null,
    processed_duration_seconds: output.durationSeconds
  }), { attempts: 5 });
}

async function failJob(job, error) {
  try {
    // Retried: a transient n8n blip must not leave the row stuck in 'processing'.
    await withRetry(() => postJson(config.failUrl, {
      worker_id: config.workerId,
      job_id: job.job_id,
      error: publicError(error)
    }), { attempts: 4 });
  } catch (reportError) {
    log.error('fail_report_failed', {
      job_id: job.job_id,
      error: publicError(reportError),
      original_error: publicError(error)
    });
  }
}

async function processJob(job) {
  const workDir = path.join(config.tmpDir, String(job.job_id));
  const started = Date.now();
  await heartbeat({ status: 'processing', job_id: job.job_id });

  try {
    validateJob(job);

    log.info('job_started', {
      job_id: job.job_id,
      job_key: job.job_key,
      job_type: job.job_type,
      part_number: job.part_number ?? null,
      session_id: job.session_id,
      cut_mode: job.cut_mode,
      start_seconds: job.start_seconds,
      end_seconds: job.end_seconds
    });

    const output = await cutVideo(job, workDir);
    log.info('ffmpeg_completed', {
      job_id: job.job_id,
      output_size_bytes: output.outputSizeBytes,
      duration_seconds: output.durationSeconds,
      requested_duration_seconds: output.requestedDurationSeconds
    });

    const uploadUrl = await createUploadSession(job, output);
    let lastPercent = -1;
    const driveItem = await uploadFileBySession(uploadUrl, output.outputPath, progress => {
      const percent = Math.round((progress.uploadedBytes / progress.totalBytes) * 100);
      if (percent === lastPercent) return;
      lastPercent = percent;
      log.info('upload_progress', {
        job_id: job.job_id,
        uploaded_bytes: progress.uploadedBytes,
        total_bytes: progress.totalBytes,
        percent
      });
    });

    await completeJob(job, output, driveItem);
    log.info('job_completed', {
      job_id: job.job_id,
      output_item_id: driveItem.id,
      elapsed_seconds: Math.round((Date.now() - started) / 1000)
    });
  } catch (error) {
    log.error('job_failed', { job_id: job.job_id, error: publicError(error) });
    await failJob(job, error);
  } finally {
    // Clips and source reads are large; never leave them behind.
    await fs.rm(workDir, { recursive: true, force: true }).catch(cleanupError => {
      log.warn('temp_cleanup_failed', { job_id: job.job_id, error: publicError(cleanupError) });
    });
    await heartbeat({ status: 'idle', job_id: null });
  }
}

async function main() {
  assertConfigComplete();

  // Clear anything a previous crash left behind before claiming new work.
  await fs.rm(config.tmpDir, { recursive: true, force: true }).catch(() => {});
  await fs.mkdir(config.tmpDir, { recursive: true });

  log.info('worker_started', {
    worker_id: config.workerId,
    worker_version: WORKER_VERSION,
    poll_interval_seconds: config.pollIntervalSeconds,
    data_dir: config.dataDir
  });

  await heartbeat({ status: 'idle', job_id: null });
  const stopHeartbeat = startHeartbeatTimer();

  while (!stopping) {
    try {
      await heartbeat({ status: 'polling', job_id: null });
      const response = await claimJob();
      const job = response?.job || (response?.job_id ? response : null);

      if (!job?.job_id) {
        await heartbeat({ status: 'idle', job_id: null });
        await sleep(config.idleBackoffSeconds * 1000);
        continue;
      }

      await processJob(job);
    } catch (error) {
      log.error('poll_cycle_failed', { error: publicError(error) });
      await heartbeat({ status: 'error', job_id: null, error: publicError(error) });
      await sleep(config.pollIntervalSeconds * 1000);
    }
  }

  stopHeartbeat();
  await heartbeat({ status: 'stopped', job_id: null });
  log.info('worker_stopped', { worker_id: config.workerId });
}

main().catch(error => {
  log.error('fatal', { error: publicError(error) });
  process.exit(1);
});
