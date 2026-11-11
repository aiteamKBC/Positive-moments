// Recording v9: choose the URL to persist for an exact match.
// Prefer the organization view link; fall back to the exact DriveItem webUrl,
// but never to a SharePoint list-form URL.
const matchedItems = $items('Exactly One Safe File?', 0, 0);

function pairedIndex(item, fallback) {
  const paired = item?.pairedItem;
  if (Array.isArray(paired)) {
    const value = Number(paired[0]?.item);
    if (Number.isInteger(value)) return value;
  }
  const value = Number(paired?.item);
  return Number.isInteger(value) ? value : fallback;
}

const usable = value => {
  const text = String(value || '').trim();
  return text && !/\/Forms\/DispForm\.aspx/i.test(text) ? text : null;
};

return items.map((item, index) => {
  const matchedIndex = pairedIndex(item, index);
  const lecture = matchedItems[matchedIndex]?.json || {};
  const response = item.json || {};
  const organizationUrl = usable(response.link?.webUrl);
  const itemUrl = usable(lecture.recording_web_url);
  const finalUrl = organizationUrl || itemUrl;

  return {
    json: {
      ...lecture,
      recording_url: finalUrl,
      recording_link_status: organizationUrl
        ? 'organization_view_link_created_exact_match'
        : (itemUrl
            ? 'drive_item_web_url_used_exact_match'
            : 'create_link_failed_no_update'),
      create_link_error: response.error?.message || null
    },
    pairedItem: { item: matchedIndex }
  };
});
