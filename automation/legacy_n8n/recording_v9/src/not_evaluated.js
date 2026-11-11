// Recording v9: lectures that failed the precheck. No Graph call, no update.
return items.map((item, index) => ({
  json: {
    ...item.json,
    recording_link_status: item.json.precheck_status,
    execution_result: 'NO_DATABASE_UPDATE_NOT_EVALUATED'
  },
  pairedItem: { item: index }
}));
