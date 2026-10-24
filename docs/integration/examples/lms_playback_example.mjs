// LMS backend example (Node.js 18+, server side only): verified recording -> short-lived Bunny embed URL.
//
// Placeholders only; every secret comes from the LMS backend's environment / secret store:
//   PUBLISHER_API_BASE       e.g. https://<positive-moments-host>/api/publisher/v1
//   PUBLISHER_SERVICE_TOKEN  the service token Kent issued to the LMS backend (never sent to a browser)
//   BUNNY_EMBED_TOKEN_KEY    library 786045 > Security > "Token authentication key" (NOT the library API key)
//
// The LMS must check enrolment BEFORE calling playableParts().
import { createHash } from "node:crypto";

const EMBED_HOST = "https://player.mediadelivery.net";
const TOKEN_TTL_SECONDS = 600;

export class NotPlayable extends Error {}

export function embedToken(tokenKey, videoId, expires) {
  // Bunny: SHA256_HEX(token_security_key + video_id + expiration), expiration in UNIX seconds.
  return createHash("sha256").update(`${tokenKey}${videoId}${expires}`, "utf8").digest("hex");
}

export async function fetchRecording(occurrenceId) {
  const base = process.env.PUBLISHER_API_BASE.replace(/\/$/, "");
  const response = await fetch(`${base}/occurrences/${encodeURIComponent(occurrenceId)}/recording`, {
    headers: { Authorization: `Bearer ${process.env.PUBLISHER_SERVICE_TOKEN}`, Accept: "application/json" },
    signal: AbortSignal.timeout(10_000),
  });
  if (response.status === 404) throw new NotPlayable("OCCURRENCE_NOT_FOUND");
  if (!response.ok) throw new Error(`publisher answered ${response.status}`); // 401/429/503: fail closed
  return response.json();
}

export async function playableParts(occurrenceId, { learnerIsEnrolled }) {
  if (!learnerIsEnrolled) throw new NotPlayable("NOT_ENROLLED");
  const answer = await fetchRecording(occurrenceId);
  if (!answer.playback.available) throw new NotPlayable(answer.playback.reason ?? answer.state);
  const key = process.env.BUNNY_EMBED_TOKEN_KEY;
  const expires = Math.floor(Date.now() / 1000) + TOKEN_TTL_SECONDS;
  return [...answer.recording.parts]
    .sort((a, b) => a.sequence - b.sequence)
    .map((part) => {
      const { library_id: library, video_id: video } = part.bunny;
      return {
        sequence: part.sequence,
        of: answer.recording.published_part_count,
        durationSeconds: part.duration_seconds,
        embedUrl: `${EMBED_HOST}/embed/${library}/${video}?token=${embedToken(key, video, expires)}&expires=${expires}`,
        teamsRecordingId: part.teams_recording_id, // = live_session_artifacts.graph_artifact_id (analytics key)
      };
    });
}
