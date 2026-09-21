// Dry run: validates configuration and the FFmpeg/FFprobe toolchain without
// claiming or processing any media job. Safe to run at any time.
import { spawn } from 'node:child_process';
import { config, missingEnv } from './config.js';
import { redactUrl } from './redact.js';

function version(command) {
  return new Promise(resolve => {
    const child = spawn(command, ['-version'], { stdio: ['ignore', 'pipe', 'ignore'] });
    let out = '';
    child.stdout.on('data', chunk => { out += chunk.toString(); });
    child.on('error', () => resolve(null));
    child.on('close', code => resolve(code === 0 ? out.split('\n')[0].trim() : null));
  });
}

const ffmpeg = await version('ffmpeg');
const ffprobe = await version('ffprobe');

const report = {
  worker_id: config.workerId || '(not set)',
  data_dir: config.dataDir,
  tmp_dir: config.tmpDir,
  endpoints: {
    claim: redactUrl(config.claimUrl),
    upload_session: redactUrl(config.uploadSessionUrl),
    complete: redactUrl(config.completeUrl),
    fail: redactUrl(config.failUrl)
  },
  worker_secret_present: Boolean(config.secret),
  upload_chunk_bytes: config.uploadChunkBytes,
  upload_chunk_is_320kib_multiple: config.uploadChunkBytes % 327680 === 0,
  precise_profile: {
    video_codec: config.preciseVideoCodec,
    preset: config.precisePreset,
    crf: config.preciseCrf,
    audio_codec: config.preciseAudioCodec,
    audio_bitrate: config.preciseAudioBitrate
  },
  ffmpeg: ffmpeg || 'NOT FOUND',
  ffprobe: ffprobe || 'NOT FOUND',
  missing_env: missingEnv
};

console.log(JSON.stringify(report, null, 2));

const ok = missingEnv.length === 0 && ffmpeg && ffprobe;
console.log(ok ? '\nCONFIG CHECK: PASS' : '\nCONFIG CHECK: INCOMPLETE');
process.exit(ok ? 0 : 1);
