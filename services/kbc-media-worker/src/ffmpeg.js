import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { config } from './config.js';
import { log } from './log.js';
import { redactText } from './redact.js';

const JOB_TYPES = new Set(['positive_clip', 'lecture_part']);
const CUT_MODES = new Set(['precise', 'fast_copy']);

function asSeconds(value, name) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) throw new Error(`${name} must be a finite number >= 0`);
  return number;
}

function safeFilename(value) {
  const base = path.basename(String(value || 'output.mp4'));
  const clean = base.replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
  return clean.toLowerCase().endsWith('.mp4') ? clean : `${clean || 'output'}.mp4`;
}

// Validates everything the worker depends on before any byte is downloaded.
export function validateJob(job) {
  if (!job || job.job_id === undefined || job.job_id === null || String(job.job_id).trim() === '') {
    throw new Error('Job is missing job_id');
  }

  const jobType = String(job.job_type || '').trim();
  if (!JOB_TYPES.has(jobType)) throw new Error(`Unsupported job_type: ${jobType || '(empty)'}`);

  const cutMode = String(job.cut_mode || '').trim();
  if (!CUT_MODES.has(cutMode)) throw new Error(`Unsupported cut_mode: ${cutMode || '(empty)'}`);

  // lecture_part is not produced yet, but the worker already accepts it so the
  // future AI segmentation feature needs no worker change.
  if (jobType === 'lecture_part') {
    const part = Number(job.part_number);
    if (!Number.isInteger(part) || part < 1 || part > 3) {
      throw new Error(`lecture_part requires part_number 1, 2 or 3 (got: ${job.part_number})`);
    }
  }

  const start = asSeconds(job.start_seconds, 'start_seconds');
  const end = asSeconds(job.end_seconds, 'end_seconds');
  if (end <= start) throw new Error('end_seconds must be greater than start_seconds');

  const sourceUrl = String(job.source_download_url || '').trim();
  if (!sourceUrl.startsWith('https://')) throw new Error('source_download_url must be an https URL');

  if (!String(job.output_filename || '').trim()) throw new Error('Job is missing output_filename');

  return {
    jobType,
    cutMode,
    start,
    end,
    duration: end - start,
    sourceUrl,
    filename: safeFilename(job.output_filename)
  };
}

export async function cutVideo(job, workDir) {
  const { cutMode, start, duration, sourceUrl, filename } = validateJob(job);

  await fs.mkdir(workDir, { recursive: true });
  const outputPath = path.join(workDir, filename);
  await fs.rm(outputPath, { force: true });

  const common = [
    '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y',
    // The source is a temporary Graph URL over HTTPS; a dropped connection
    // part-way through a 1 GB read must not fail the whole job.
    '-reconnect', '1',
    '-reconnect_streamed', '1',
    '-reconnect_on_network_error', '1',
    '-reconnect_delay_max', '30',
    '-rw_timeout', '60000000',
    // Input seeking: FFmpeg issues an HTTP range request instead of reading the
    // whole recording, and still decodes accurately when re-encoding.
    '-ss', start.toFixed(3),
    '-i', sourceUrl,
    '-t', duration.toFixed(3),
    '-map', '0:v:0?', '-map', '0:a:0?'
  ];

  let args;
  if (cutMode === 'precise') {
    args = [
      ...common,
      '-c:v', config.preciseVideoCodec,
      '-preset', config.precisePreset,
      '-crf', config.preciseCrf,
      '-c:a', config.preciseAudioCodec,
      '-b:a', config.preciseAudioBitrate,
      '-movflags', '+faststart',
      outputPath
    ];
  } else {
    args = [
      ...common,
      '-c', 'copy',
      '-avoid_negative_ts', 'make_zero',
      '-movflags', '+faststart',
      outputPath
    ];
  }

  await runProcess('ffmpeg', args, config.ffmpegTimeoutMinutes * 60 * 1000);

  const stat = await fs.stat(outputPath);
  if (stat.size < 1024) throw new Error(`FFmpeg output is unexpectedly small: ${stat.size} bytes`);

  const measured = await probeDurationSeconds(outputPath);
  if (measured !== null) {
    // A stream-copy cut lands on keyframes, so allow a wider tolerance there.
    const tolerance = cutMode === 'precise' ? Math.max(1, duration * 0.02) : Math.max(10, duration * 0.1);
    if (Math.abs(measured - duration) > tolerance) {
      throw new Error(
        `Output duration ${measured.toFixed(3)}s differs from requested ${duration.toFixed(3)}s by more than ${tolerance.toFixed(3)}s`
      );
    }
  }

  return {
    outputPath,
    outputFilename: filename,
    outputSizeBytes: stat.size,
    durationSeconds: Number((measured ?? duration).toFixed(3)),
    requestedDurationSeconds: Number(duration.toFixed(3))
  };
}

// Returns null when ffprobe is unavailable or cannot read a duration, so the
// job is never failed just because the probe is missing.
export async function probeDurationSeconds(filePath) {
  try {
    const { stdout } = await runProcess('ffprobe', [
      '-v', 'error',
      '-show_entries', 'format=duration',
      '-of', 'default=noprint_wrappers=1:nokey=1',
      filePath
    ], 60000, { captureStdout: true });
    const value = Number(String(stdout).trim());
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch (error) {
    log.warn('ffprobe_unavailable', { error: String(error?.message || error).slice(0, 500) });
    return null;
  }
}

function runProcess(command, args, timeoutMs, { captureStdout = false } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', captureStdout ? 'pipe' : 'ignore', 'pipe'] });
    let stderr = '';
    let stdout = '';
    let settled = false;
    let timedOut = false;

    const timer = setTimeout(() => {
      if (!settled) {
        timedOut = true;
        child.kill('SIGKILL');
      }
    }, timeoutMs);

    if (captureStdout) {
      child.stdout.on('data', chunk => { stdout += chunk.toString(); });
    }
    child.stderr.on('data', chunk => {
      stderr += chunk.toString();
      if (stderr.length > 20000) stderr = stderr.slice(-20000);
    });

    child.on('error', error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });

    child.on('close', code => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (timedOut) {
        reject(new Error(`${command} timed out after ${Math.round(timeoutMs / 1000)}s`));
        return;
      }
      if (code === 0) resolve({ stdout, stderr });
      // stderr repeats the full command line, including the signed source URL.
      else reject(new Error(`${command} exited with code ${code}: ${redactText(stderr).trim()}`));
    });
  });
}
