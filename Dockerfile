FROM python:3.12-slim

# minimal system bits — git is the only useful extra for the agent's `git` action
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# fixed UID so host bind-mount permissions are predictable
RUN groupadd -r -g 10001 mako \
 && useradd -r -u 10001 -g 10001 -m -d /home/mako -s /bin/bash mako

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tick.py digest.py supervisor.py analyse.py ./
COPY prompts/ ./prompts/
COPY seed/ ./seed/

# /data is the mount point for state, notes, archive, logs, pending, and config.yaml
RUN mkdir -p /data \
 && chown -R mako:mako /app /data

USER mako

ENV MAKO_ROOT=/data \
    MAKO_CONFIG=/data/config.yaml \
    TZ=Europe/London \
    PYTHONUNBUFFERED=1

# healthcheck: tick_counter must have been bumped in the last 10 minutes once running.
# Allows a 3-minute grace on first boot.
HEALTHCHECK --interval=2m --timeout=5s --start-period=3m --retries=3 \
  CMD python3 -c "import os, time, sys; \
p='/data/state/tick_counter.txt'; \
sys.exit(0 if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < 600 else 1)"

CMD ["python3", "/app/supervisor.py", "--config", "/data/config.yaml"]
