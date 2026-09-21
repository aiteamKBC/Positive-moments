from app.graph.client import Phase1GraphClient


def build_graph_client(settings) -> Phase1GraphClient:
    return Phase1GraphClient(
        tenant_id=settings.graph_tenant_id,
        client_id=settings.graph_client_id,
        client_secret=settings.graph_client_secret,
        scope=settings.graph_scope,
        base_url=settings.graph_base_url,
    )
