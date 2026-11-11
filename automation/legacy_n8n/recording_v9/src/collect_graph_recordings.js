// Recording v9: classify each organizer-aware Graph recordings lookup.
//
// The HTTP node runs once per lecture that passed the precheck, in order, and
// continues on error, so response k belongs to lookup item k. A failed HTTP
// call (401/403/404/429/5xx/network) is GRAPH_LOOKUP_FAILED - it proves
// nothing about whether a recording exists. RECORDING_NOT_FOUND is reserved
// for a successful, complete Graph response with no recording for the call id.
const lectures = $items('Graph Lookup Possible?', 0, 0);

function httpStatus(error) {
  const direct = Number(error?.httpCode ?? error?.status ?? error?.statusCode);
  if (Number.isInteger(direct) && direct >= 100) return direct;
  const match = /^\s*(\d{3})\b/.exec(String(error?.message || ''));
  return match ? Number(match[1]) : null;
}

if (items.length !== lectures.length) {
  throw new Error(`Graph lookup returned ${items.length} responses for ${lectures.length} lectures`);
}

const result = items.map((item, index) => {
  const lecture = lectures[index]?.json || {};
  const response = item.json || {};
  const expected = String(lecture.expected_call_id || '').toLowerCase();
  const base = {
    ...lecture,
    graph_http_status: null,
    graph_recording_id: null,
    graph_created_at: null,
    graph_content_correlation_id: null,
    graph_recordings_returned: null
  };

  if (response.error) {
    return { ...base, graph_http_status: httpStatus(response.error), graph_status: 'GRAPH_LOOKUP_FAILED' };
  }
  if (!Array.isArray(response.value)) {
    return { ...base, graph_status: 'GRAPH_LOOKUP_FAILED', graph_failure: 'response_has_no_value_array' };
  }
  const recordings = response.value;
  if (response['@odata.nextLink']) {
    // A later page could hold a second recording for the same call.
    return { ...base, graph_recordings_returned: recordings.length, graph_status: 'GRAPH_RESULTS_INCOMPLETE' };
  }
  const matches = recordings.filter(recording =>
    String(recording?.callId || '').toLowerCase() === expected);

  let status = 'EXACT_GRAPH_RECORDING_FOUND';
  if (!expected) status = 'SESSION_CALL_ID_MISSING';
  else if (matches.length === 0) status = 'RECORDING_NOT_FOUND';
  else if (matches.length > 1) status = 'AMBIGUOUS_GRAPH_RECORDINGS';
  else if (!Number.isFinite(Date.parse(matches[0].createdDateTime || ''))) {
    status = 'GRAPH_RECORDING_TIMESTAMP_INVALID';
  }

  const exact = status === 'EXACT_GRAPH_RECORDING_FOUND' ? matches[0] : null;
  return {
    ...base,
    graph_recordings_returned: recordings.length,
    graph_recording_id: exact?.id || null,
    graph_created_at: exact?.createdDateTime || null,
    graph_content_correlation_id: exact?.contentCorrelationId || null,
    graph_status: status
  };
});

return [{ json: { lectures: result, lecture_count: result.length } }];
