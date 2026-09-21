# Phase 1.5 real shadow validation

Status: **superseded** — see [phase1_final_hardening.md](phase1_final_hardening.md) (**PHASE_1_FINAL_READY**).

Historical status at the time of writing: PHASE_1_BLOCKED.

Validation date: 2026-09-14. Target Cairo business date: 2026-09-04.

## Hardening completed

- Database roles are explicit: `DATABASE_URL` is the KBC application database; `APTEM_DATABASE_URL` is the separate Aptem source database.
- Aptem connections run in a PostgreSQL-enforced read-only transaction. There is no fallback to the KBC URL, and equal configured URLs are rejected.
- `APTEM_DATABASE_URL` is configured locally and distinct from the KBC connection; its example remains blank/safe.
- Calendar requests send `Prefer: IdType="ImmutableId"`. The existing UUIDv5 owner + `iCalUId` identity is unchanged; no registry rows existed to migrate.
- Registry upserts now report created versus updated, discovery summaries report total date rows and unmatched reasons, and read-only QA evidence reports meeting-ID and normalized-subject set differences.
- A correct Aptem connection returning zero active groups is an explicit safety stop. The read-only validator reports role, connection success, table existence, total rows, active rows, and SQL status without connection details.

## Real-run result

Both database and calendar settings are configured. The correct Aptem source connection succeeded in enforced read-only mode: the table exists, the SQL succeeded, there are 680 source rows, and 43 distinct active groups remain after the mandated filter.

App-only Graph authentication, the immutable-ID calendar request, and explicit calendar-user lookup succeeded. The calendar returned 20 events, of which 19 had Teams Join URLs and were not cancelled/non-Teams entries. Online-meeting resolution using the production-equivalent configured mailbox context stopped with:

`graph_permission_error`, HTTP 403, provider code `Forbidden`

The app token's roles claim contains both `OnlineMeetings.Read.All` and `OnlineMeetings.ReadWrite.All`. Microsoft requires a Teams application access policy assigned to the user specified in `/users/{userId}/onlineMeetings`. After the administrator reported the policy corrected, repeated fresh-token requests still returned the same 403. The policy is therefore not yet effective for this exact app/mailbox pairing, whether because of propagation or assignment scope.

A diagnostic organizer-context run completed but is not accepted as production-equivalent: it resolved 6 of 19 meetings and produced zero discovery matches. The six resolved entries were unrelated/test subjects; the 13 unresolved entries included every QA-matching subject. A separate read-only comparison proved all six QA subjects normalize to exact matches among the 43 active Aptem groups. Thus the matching rule is correct; the zero discovery-match result is downstream of missing meeting IDs. No discrepancy was changed.

No persisted discovery run was attempted because the required mailbox-context dry run failed. Registry and discovery-run counts for the date remain zero. Existing KBC QA evidence remains six sessions, six non-null/distinct meeting IDs, and six distinct normalized subjects.

## Test result

`python -m pytest -q`: 32 passed. Tests include immutable-header propagation, exact filter encoding, sanitized access-policy classification, explicit user context, alias/not-found handling, distinct database connection boundaries, database-enforced read-only access, zero-group safety, exact QA/Aptem comparison, created/updated accounting, rollback-isolated database upserts, and QA comparison behavior.

## Remaining external action

A Microsoft Teams administrator must verify/create an application access policy that includes this app registration and grant it to the configured calendar mailbox, then allow for policy propagation. After that, repeat the dry run before any persistence. No tenant policy was changed by this task.
