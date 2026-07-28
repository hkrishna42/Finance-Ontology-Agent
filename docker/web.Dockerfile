# syntax=docker/dockerfile:1.7
# Firm Ontology Platform — web image. Builds the Vite/React SPA, then serves it from nginx with a
# same-origin reverse proxy to the api service (see docker/nginx.conf).

# ---- build: compile the SPA (tsc -b && vite build) --------------------------------------------
# Base images pinned by digest (tags kept for readability).
FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS build

WORKDIR /web

# Dependency layer first (cache-friendly): lockfile only, then a clean, reproducible install.
COPY web/package.json web/package-lock.json ./
RUN npm ci

# App source (node_modules is .dockerignored, so the install above is preserved).
COPY web/ ./
RUN npm run build

# ---- serve: nginx static + reverse proxy ------------------------------------------------------
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 AS runtime

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html

EXPOSE 80

# Liveness: nginx is serving the SPA shell (the api has its own healthcheck).
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -qO /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
