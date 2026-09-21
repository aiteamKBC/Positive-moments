import pytest

from app.config.settings import Settings


def configured(**overrides):
    values = {
        "database_url": "postgresql://kbc.invalid/db",
        "aptem_database_url": "postgresql://aptem.invalid/db",
        "graph_tenant_id": "tenant", "graph_client_id": "client",
        "graph_client_secret": "secret",
        "graph_scope": "https://graph.microsoft.com/.default",
        "graph_base_url": "https://graph.microsoft.com/v1.0",
        "calendar_user_upn": "calendar@example.invalid",
    }
    values.update(overrides)
    return Settings(**values)


def test_discovery_requires_both_external_role_settings():
    with pytest.raises(ValueError) as error:
        configured(aptem_database_url="", calendar_user_upn="").require_discovery()
    assert "APTEM_DATABASE_URL" in str(error.value)
    assert "KBC_LECTURE_CALENDAR_USER_UPN" in str(error.value)


def test_aptem_role_cannot_silently_reuse_kbc_url():
    with pytest.raises(ValueError, match="separate Aptem source"):
        configured(aptem_database_url="postgresql://kbc.invalid/db").require_discovery()
