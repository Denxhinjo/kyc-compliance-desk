# The worker service, and the image the release phase runs migrations from.
#
# Built from the REPOSITORY ROOT. heroku.yml sets the context, so the paths
# below are repo-relative.
#
# The root context is not a convenience here, it is required: the worker reads
# the sanctions list from /data, which lives outside /worker precisely because
# it belongs to neither service. Building from /worker alone would produce an
# image that starts cleanly and then fails the first time anything is screened.
#
# This is a LONG-RUNNING PROCESS, not a request handler. It polls, holds a
# connection, reclaims abandoned jobs and reschedules its own sweeps. That is
# why the platform had to support an always-on service at all — a serverless
# function would have meant rewriting the part of this project that is most
# worth reading.

FROM python:3.12-slim

# PYTHONUNBUFFERED so logs reach `heroku logs` immediately rather than sitting
# in a buffer until the process exits — which, for a process designed never to
# exit, means never.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so editing worker code does not reinstall psycopg.
COPY worker/requirements.txt ./worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

COPY worker/ ./worker/

# The sanctions list. Only the synthetic fixture is in the repository; OFAC is
# gitignored, so this image ships WITHOUT it and the worker downloads it at boot
# (screening/sources.ensure_available). If that download fails, SANCTIONS_FALLBACK
# makes the worker run degraded against the fixture rather than crash-loop.
COPY data/ ./data/

# The downloader's parser, shared rather than duplicated into the worker.
COPY scripts/ ./scripts/

# The migrations, because the release phase runs from THIS image. Putting them
# here rather than in the web image keeps to the rule that /db belongs to
# neither service — the worker is merely the thing holding the psycopg driver,
# not the owner of the schema.
COPY db/ ./db/

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 worker && chown -R worker:worker /app
USER worker

# data/ must stay writable: the OFAC list is downloaded into it at boot.
WORKDIR /app/worker
CMD ["python", "main.py"]
