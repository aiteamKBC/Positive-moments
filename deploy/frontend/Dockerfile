# ---------------------------------------------------------------------------
# QA Core: the Kent Business College / Lecture Intelligence console.
#
# Two stages: Node builds the Vite bundle, nginx serves the static result. The
# build tooling never reaches the production image.
#
# No credential is baked in. `VITE_API_BASE_URL` is a PATH the browser calls
# (default `/api`), resolved by whatever reverse proxy the infrastructure team
# puts in front - which is deliberately NOT configured here.
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS build

WORKDIR /build

# Dependencies first so a source-only change reuses the cached install layer.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

COPY frontend/ ./

# Same-origin by default: the browser calls /api and the proxy routes it to the
# api service. Overridable at build time for a split-origin deployment.
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build


FROM nginx:1.27-alpine

# A single-page app: every unknown path must return index.html or a browser
# refresh on /operations/runs would 404 instead of loading the router.
COPY deploy/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80
