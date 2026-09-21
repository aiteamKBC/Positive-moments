FROM node:22-bookworm-slim

# FFmpeg supplies both ffmpeg and ffprobe; tini reaps the FFmpeg child process
# so a stopped container does not leave a transcode running.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

ENV NODE_ENV=production \
    DATA_DIR=/app/data \
    TMP_DIR=/app/data/tmp

WORKDIR /app
COPY package.json ./
COPY src ./src

# No dependencies to install: the worker uses only the Node standard library.
RUN mkdir -p /app/data/tmp && chown -R node:node /app

# This is a pull worker, not a server. No port is exposed.
USER node

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["node", "src/healthcheck.js"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "src/worker.js"]
