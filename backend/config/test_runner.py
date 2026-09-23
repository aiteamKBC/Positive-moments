"""
The only test runner this project's Django tests may use.

`settings.py` already refuses to build a test database from `DATABASE_URL`;
this re-checks the final connection settings immediately before Django creates
anything, so a runner invoked another way (a custom settings module, an IDE, a
`--settings` override, pytest-django) cannot reach a production server either.
"""
from django.test.runner import DiscoverRunner

from config.test_database import verify_connection_is_not_production


class SafeDatabaseTestRunner(DiscoverRunner):
    """DiscoverRunner that proves its database is not production first."""

    def setup_databases(self, **kwargs):
        from django.db import connections

        for alias in connections:
            verify_connection_is_not_production(connections[alias].settings_dict)
        return super().setup_databases(**kwargs)
