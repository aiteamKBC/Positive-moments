"""Assemble the P0-C recording worker from the platform's existing app-only Graph client (read-only use)."""
from __future__ import annotations

from app.publisher.recordings import RecordingPolicy, RecordingWorker
from app.recordings.drive_items import CandidateMetadataReader
from app.recordings.factory import build_discovery, resolution_policy


def build_recording_worker(settings, *, connect, graph=None, policy: RecordingPolicy | None = None) -> RecordingWorker:
    """
    Reads only: callRecording metadata, tenant search, OneDrive / channel
    folder listings, one driveItem's metadata. RecordingLinkPublisher
    (createLink, a Graph write) is deliberately not built.
    """
    if graph is None:
        from app.graph.auth import build_graph_client
        graph = build_graph_client(settings)
    region = settings.recording_link_search_region
    return RecordingWorker(connect=connect, graph=graph,
                           discovery_factory=lambda: build_discovery(graph, search_region=region),
                           evidence_reader=CandidateMetadataReader(graph), policy=policy,
                           resolution_policy=resolution_policy(settings))
