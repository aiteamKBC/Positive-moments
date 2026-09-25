// Recording v9: match every lecture to AT MOST one recording file.
//
// Unchanged from v8: exact Graph call-id linkage, exact normalized subject,
// exactly one candidate, ambiguous => no update.
// Added: the file must be named for the lecture's own date.
// Changed: the timestamp rule. A Teams file name carries the moment recording
// started on the client, which the audit measured 17-73 s BEFORE Graph's
// createdDateTime (and never after it). So the file may lead Graph by up to
// 120 s and may never trail it. The window is a constant, not a setting.
const MAX_FILE_LEAD_SECONDS = 120;
const MATCH_METHOD = 'exact_call_id_subject_timestamp_v9';

const lectures =
  $items('Collect Lectures and Exact Graph Times')[0]?.json?.lectures || [];
const tenantFiles =
  $items('Collect Tenant Search Candidates')[0]?.json?.tenant_search_files || [];
const channelFiles =
  $items('Collect All Channel Recording Files')[0]?.json?.channel_recording_files || [];
const oneDriveResponse = items[0]?.json || {};
const oneDriveFiles = (Array.isArray(oneDriveResponse.value) ? oneDriveResponse.value : [])
  .filter(file => file?.id && file?.file && /\.mp4$/i.test(String(file.name || '')))
  .map(file => ({
    id: file.id,
    driveId: file?.parentReference?.driveId || null,
    name: file.name,
    webUrl: file.webUrl || null,
    createdDateTime: file.createdDateTime || null,
    lastModifiedDateTime: file.lastModifiedDateTime || null,
    source: 'current_user_onedrive_recordings'
  }));

const deduped = new Map();
for (const file of [...tenantFiles, ...channelFiles, ...oneDriveFiles]) {
  if (!file?.id || !file?.driveId) continue;
  const key = String(file.driveId) + ':' + String(file.id);
  deduped.set(key, { ...(deduped.get(key) || {}), ...file });
}
const files = [...deduped.values()];

// Same subject normalization as v8 and QA One Lecture v8.
const normalizeSubject = value => String(value || '')
  .toLowerCase()
  .replace(/&amp;/g, '&')
  .replace(/[^a-z0-9]+/g, ' ')
  .replace(/\s+/g, ' ')
  .trim()
  .replace(/^(dr|doctor|prof|professor|mr|mrs|ms)\s+/, '')
  .replace(/[^a-z0-9]+/g, '');

function parseTeamsFilename(name) {
  const match = String(name || '').match(
    /^(.*)-(\d{8})_(\d{6})(?:UTC)?-Meeting Recording\.mp4$/i
  );
  if (!match) return null;
  const date = match[2];
  const time = match[3];
  const isoDate = date.slice(0, 4) + '-' + date.slice(4, 6) + '-' + date.slice(6, 8);
  const timestamp = Date.parse(isoDate + 'T' + time.slice(0, 2) + ':' +
    time.slice(2, 4) + ':' + time.slice(4, 6) + 'Z');
  if (!Number.isFinite(timestamp)) return null;
  return { subject: match[1], isoDate, timestamp };
}

const parsedFiles = files
  .map(file => ({ file, parsed: parseTeamsFilename(file.name) }))
  .filter(candidate => candidate.parsed);

return lectures.map((lecture, index) => {
  const lectureDate = String(lecture.lecture_date || lecture.date || '').slice(0, 10);
  const expectedSubject = normalizeSubject(lecture.subject);
  const graphTimestamp = Date.parse(lecture.graph_created_at || '');
  const graphExact = lecture.graph_status === 'EXACT_GRAPH_RECORDING_FOUND' &&
    Number.isFinite(graphTimestamp) && Boolean(expectedSubject) && Boolean(lectureDate);

  const assessed = graphExact ? parsedFiles.map(candidate => {
    const leadSeconds = (graphTimestamp - candidate.parsed.timestamp) / 1000;
    return {
      ...candidate,
      subject_exact: normalizeSubject(candidate.parsed.subject) === expectedSubject,
      same_date: candidate.parsed.isoDate === lectureDate,
      lead_seconds: leadSeconds,
      timestamp_ok: leadSeconds >= 0 && leadSeconds <= MAX_FILE_LEAD_SECONDS
    };
  }) : [];

  const exact = assessed
    .filter(c => c.subject_exact && c.same_date && c.timestamp_ok)
    .sort((a, b) => a.lead_seconds - b.lead_seconds);

  let status;
  if (!graphExact) status = lecture.graph_status || 'GRAPH_LOOKUP_FAILED';
  else if (exact.length > 1) status = 'AMBIGUOUS_RECORDING_FILES';
  else if (exact.length === 1) status = 'EXACT_RECORDING_FILE_MATCHED';
  else if (assessed.some(c => c.subject_exact && c.same_date)) status = 'TIMESTAMP_MISMATCH';
  else if (assessed.some(c => c.same_date && c.timestamp_ok)) status = 'SUBJECT_MISMATCH';
  else status = 'RECORDING_FILE_NOT_FOUND';

  const best = status === 'EXACT_RECORDING_FILE_MATCHED' ? exact[0] : null;
  const file = best?.file || null;
  const nearestSubjectLead = assessed
    .filter(c => c.subject_exact && c.same_date)
    .map(c => c.lead_seconds)
    .sort((a, b) => Math.abs(a) - Math.abs(b))[0];

  return {
    json: {
      ...lecture,
      recording_item_id: file?.id || null,
      recording_drive_id: file?.driveId || null,
      recording_filename: file?.name || null,
      recording_web_url: file?.webUrl || null,
      recording_source: file?.source || null,
      recording_team_name: file?.team_name || null,
      recording_channel_name: file?.channel_name || null,
      timestamp_difference_seconds: best ? best.lead_seconds : null,
      nearest_same_subject_lead_seconds: nearestSubjectLead ?? null,
      max_file_lead_seconds: MAX_FILE_LEAD_SECONDS,
      exact_file_matches: exact.length,
      candidate_files_seen: files.length,
      recording_match_method: best ? MATCH_METHOD : null,
      ambiguous_candidate_filenames: exact.length > 1
        ? exact.map(candidate => candidate.file?.name || null) : [],
      recording_link_status: status
    },
    pairedItem: { item: index }
  };
});
