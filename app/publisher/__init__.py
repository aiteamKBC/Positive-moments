"""
Continuous Bunny Publisher workers (P0-B onwards).

Nothing in this package imports the QA, writer, rendering, Perfect Lecture or
model-provider modules, and nothing here needs an OpenAI key, the n8n
precheck, the nightly cycle or a legacy qa_doctors_sessions row. A test
asserts that import boundary.
"""
