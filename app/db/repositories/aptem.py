from app.common.errors import ACTIVE_GROUP_QUERY_ERROR, PlatformError


ACTIVE_GROUPS_SQL = '''
SELECT DISTINCT "Group"
FROM public.aptem_auto_extracting
WHERE "Group" IS NOT NULL
  AND btrim("Group") <> ''
  AND lower(coalesce("Program-Status", '')) = 'active'
'''


class AptemRepository:
    def load_active_groups(self, connection) -> list[str]:
        try:
            return [row[0] for row in connection.execute(ACTIVE_GROUPS_SQL).fetchall()]
        except Exception as exc:
            raise PlatformError(ACTIVE_GROUP_QUERY_ERROR, "active Aptem group query failed") from exc

