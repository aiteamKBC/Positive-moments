"""Assemble the RECORDING_LINK service from the platform's existing app-only Graph client."""
from __future__ import annotations

from app.db.repositories.recording_links import RecordingLinkRepository
from app.recordings.drive_items import (
    ChannelRecordingsFolder,
    DriveItemDiscovery,
    OneDriveRecordingsFolder,
    RecordingLinkPublisher,
    TenantRecordingSearch,
)
from app.recordings.graph_lookup import RecordingMetadataGateway
from app.recordings.service import RecordingLinkService


def build_discovery(graph, *, search_region: str | None) -> DriveItemDiscovery:
    return DriveItemDiscovery([
        TenantRecordingSearch(graph, region=search_region),
        OneDriveRecordingsFolder(graph),
        ChannelRecordingsFolder(graph),
    ])


def build_recording_link_service(settings, *, graph=None, discovery=None,
                                 with_publisher: bool = True) -> RecordingLinkService:
    """
    One MICROSOFT_GRAPH_* client-credentials client (app/graph/auth.py) serves
    the metadata lookup, discovery and publishing. No second app, no delegated
    credential, nothing from n8n.
    """
    if graph is None:
        from app.graph.auth import build_graph_client
        graph = build_graph_client(settings)
    return RecordingLinkService(
        repository=RecordingLinkRepository(),
        metadata_gateway=RecordingMetadataGateway(graph),
        discovery=discovery or build_discovery(
            graph, search_region=settings.recording_link_search_region),
        publisher=(RecordingLinkPublisher(
            graph, create_links=settings.recording_link_create_org_links)
            if with_publisher else None))
