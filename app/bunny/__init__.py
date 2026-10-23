"""
Microsoft Teams recordings and transcripts -> Bunny Stream.

The recording travels Microsoft -> Bunny directly (URL Fetch of a fresh Graph
download URL); this platform never downloads or proxies it. Captions are the
stored Teams WebVTT (lecture_transcript_artifact_contents.raw_content) with
speaker tags removed - no OpenAI, no Bunny AI transcribing.
"""
