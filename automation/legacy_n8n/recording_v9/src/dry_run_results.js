// Recording v9: dry-run report. Nothing downstream of this node writes.
return items.map((item, index) => ({
  json: {
    ...item.json,
    execution_result:
      item.json.recording_link_status === 'EXACT_RECORDING_FILE_MATCHED'
        ? 'DRY_RUN_EXACT_MATCH_NO_DATABASE_UPDATE'
        : 'DRY_RUN_UNMATCHED_NO_DATABASE_UPDATE'
  },
  pairedItem: { item: index }
}));
