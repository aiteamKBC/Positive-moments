FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Production dependencies only. See automation/scheduler/requirements.txt for
# why this is not backend/requirements.txt and not requirements-dev.txt.
COPY automation/scheduler/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Only the application package and its migrations. The frontend, the logs and
# the media worker are never visible to this container.
COPY app ./app

# A scheduler, not a server. No port is exposed.
RUN useradd --create-home --uid 10001 scheduler && chown -R scheduler /app
USER scheduler

# One long-running process that sleeps between cycles and calls the SAME
# entrypoint the CLI and the validated pilots use.
CMD ["python", "-m", "app.cli.main", "scheduler-daemon"]
