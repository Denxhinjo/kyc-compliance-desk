# The worker service.
#
# Built from the REPOSITORY ROOT — set RAILWAY_DOCKERFILE_PATH to
# worker/Dockerfile on the Railway service.
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

# PYTHONUNBUFFERED so logs appear in Railway immediately rather than sitting in
# a buffer until the process exits — which, for a process designed never to
# exit, means never.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so editing worker code does not reinstall psycopg.
COPY worker/requirements.txt ./worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

COPY worker/ ./worker/
# The sanctions list. Only the synthetic fixture is in the repository; the OFAC
# download is gitignored, so a deployed worker screens against the committed
# fixture unless someone adds the real file to the image deliberately.
COPY data/ ./data/

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 worker && chown -R worker:worker /app
USER worker

WORKDIR /app/worker
CMD ["python", "main.py"]
