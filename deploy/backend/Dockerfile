# ---------------------------------------------------------------------------
# QA Core: the Django/DRF API.
#
# Serves the Operations API the console reads. It is NOT the scheduler: nothing
# in this image runs a nightly cycle, and the two are separate services so that
# restarting the API cannot interrupt a cycle mid-write.
#
# The media worker is not part of this image and not part of this release.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Django, DRF and CORS - the API's dependencies, plus gunicorn to serve it.
# `runserver` is a development server and is never the production entrypoint.
COPY backend/requirements.txt ./backend-requirements.txt
RUN pip install --no-cache-dir -r backend-requirements.txt gunicorn==23.0.0

# The platform package: the API imports the same orchestration, repositories
# and settings the scheduler and the CLI use, so there is exactly one
# implementation of every QA rule.
COPY app ./app
COPY backend ./backend

# Africa/Cairo business dates are computed in Python, not by the container
# clock, but a correct TZ keeps logs readable next to the scheduler's.
ENV TZ=Africa/Cairo

RUN useradd --create-home --uid 10002 api && chown -R api /app
USER api

EXPOSE 8000
WORKDIR /app/backend

# 3 workers is sized for an internal operations console, not public traffic.
# The Operations day report is a slow read (~12s against managed Postgres), so
# the timeout is generous on purpose; a shorter one would kill real requests.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
