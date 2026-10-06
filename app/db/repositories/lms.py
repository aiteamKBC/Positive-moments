from app.common.errors import LMS_MODULE_QUERY_ERROR, PlatformError
from app.db.connection import readonly_database_connection


MODULE_TITLES_SQL = '''
SELECT DISTINCT BTRIM(title) AS title
FROM curriculum.modules
WHERE title IS NOT NULL
  AND BTRIM(title) <> ''
  AND deleted_at IS NULL
  AND NOT COALESCE(is_programme_deleted, false)
ORDER BY 1
'''


class LmsModuleRepository:
    """
    Module titles from the LMS, a second eligibility source beside Aptem.

    Read-only, and it opens its own short connection: discovery reads it once
    per day it discovers, then lets go.
    """

    def __init__(self, database_url: str):
        self._database_url = database_url

    def load_module_titles(self) -> list[str]:
        try:
            with readonly_database_connection(self._database_url) as connection:
                return [row[0] for row in connection.execute(MODULE_TITLES_SQL).fetchall()]
        except Exception as exc:
            # The cause is not chained into the message: a connection error
            # can quote the connection string.
            raise PlatformError(LMS_MODULE_QUERY_ERROR,
                                f"LMS module query failed ({type(exc).__name__})") from None
