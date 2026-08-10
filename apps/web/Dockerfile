# PublishHub web — production image (Requirements 4.7, 6.2, 6.3, 6.6).
#
# Two stages with a hard boundary between them: Node builds the bundle, nginx
# serves it, and the runtime image has no Node in it at all. That is Requirement
# 6.2 at its most literal — the final layer holds static files, an nginx binary,
# and one shell script, so there is no npm, no Vite, no TypeScript, and no
# JavaScript runtime to keep patched.
#
# Four properties are load-bearing at deploy time and easy to break by accident:
#
# 1. **Runtime configuration, not build-time.** The bundle is built once and
#    contains no API URL. At container start the entrypoint writes
#    /usr/share/nginx/runtime/config.js from `API_BASE_URL`, and nginx serves it
#    at `/config.js`; index.html loads it before the module bundle, so
#    `window.__PUBLISHHUB_CONFIG__` is set by the time React mounts
#    (Requirement 4.7). One image, every environment. The alternative — baking
#    `VITE_API_URL` into the bundle — would mean an image per environment and a
#    rebuild to change a hostname, and it is the thing this design exists to
#    avoid.
#
#    nginx also proxies `/api` to the API Service, which is why the default
#    `API_BASE_URL` is the relative `/api`: every request the browser makes is
#    same-origin, so CORS never enters the picture in normal operation.
#
# 2. **Non-root on a high port.** `nginxinc/nginx-unprivileged` runs as uid 101
#    and listens on 8080, because a non-root process cannot bind 80
#    (Requirement 6.3). Every application file — the assets and the nginx config
#    — is root-owned, so the runtime user reads them and writes none of them.
#
# 3. **Exactly one writable path.** /usr/share/nginx/runtime, owned by uid 101,
#    holds the generated config.js and nothing else. Keeping it out of
#    /usr/share/nginx/html is what lets the asset directory stay unwritable; a
#    generated file living next to the assets would mean the served bundle is
#    writable by the process serving it.
#
#    Under `readOnlyRootFilesystem: true` this image needs writable mounts at
#    /usr/share/nginx/runtime (the generated config), /etc/nginx/conf.d (the
#    rendered template), /tmp (nginx's pid file and proxy temp paths, per the base
#    image's nginx.conf) and /var/cache/nginx. emptyDirs plus an `fsGroup` cover
#    all four; the entrypoint fails with a named path if one of them is missing or
#    not writable, rather than starting a container that serves the wrong thing.
#
# 4. **A failed render is an outage, not a wrong page.** The base image's
#    /etc/nginx/conf.d/default.conf is deleted here, and its welcome page with it.
#    That matters because 20-envsubst-on-templates.sh logs an error and continues
#    when it cannot write its output: had the stock default.conf still been there,
#    an unwritable conf.d would have produced a healthy container serving the
#    nginx welcome page. With conf.d empty, nginx has no server block, binds
#    nothing, and fails its probes — a loud failure instead of a silent one.
#
# Signals: ENTRYPOINT and CMD are inherited from the base image, whose
# /docker-entrypoint.sh ends in `exec "$@"`, so nginx is PID 1 and receives
# signals directly. Note the base image's `STOPSIGNAL SIGQUIT` (nginx's graceful
# shutdown) is honored by Docker but not by Kubernetes, which always sends
# SIGTERM — nginx's fast shutdown. For a static server with sub-millisecond
# responses that costs nothing; the chart's answer for draining connections during
# a rollout is a `preStop` sleep, not a signal change here.
#
# Build from this directory:
#   docker build -t publishhub-web:dev apps/web
# `make apps-build` (spec task 7.3) does this for all three services and tags each
# with both `latest` and the Git commit SHA.

# Pinned, not floating (Requirement 6.6). The Node tag matches apps/api/Dockerfile
# and docker-compose.yaml so the whole repository builds on one interpreter; the
# nginx tag pins the nginx patch version *and* the Alpine version, because
# `1.30-alpine` would drift on both.
ARG NODE_IMAGE=node:24.19.0-bookworm-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.30.4-alpine3.24

# The npm that wrote package-lock.json, and therefore the only npm guaranteed to
# read it with `npm ci`. Which packages npm considers part of the tree changes
# between npm releases, so `npm ci` rejects a lockfile written by a different one;
# the resolver is part of the toolchain that has to be pinned, exactly like the
# interpreter. See the longer note in apps/api/Dockerfile.
#
# If this build starts failing with "Missing: … from lock file", the lockfile was
# rewritten by a different npm — `npm install` does that, including the one the
# web service runs at start in docker-compose.yaml — and reconciling the two is
# the fix, not switching this file to `npm install`.
ARG NPM_VERSION=11.6.2

# ---------------------------------------------------------------------------
# Stage 1: build the static bundle.
#
# Dependencies install from the lockfile in their own layer, above the source
# copy, so editing a component does not re-resolve the tree. Dev dependencies are
# installed and that is not a compromise: Vite, esbuild and the React plugin *are*
# the build, and none of them cross into stage two.
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS build

WORKDIR /app

# Noise suppression, not behavior. The audit and funding steps reach the network
# for information nobody reads in a build log, and the update notifier prints a
# box telling a container to upgrade its own npm.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

ARG NPM_VERSION
RUN npm install --global "npm@${NPM_VERSION}"

COPY package.json package-lock.json ./

# `npm ci` rather than `npm install`: it installs exactly what package-lock.json
# records and fails if the lockfile and package.json disagree, which is what makes
# the build reproducible (Requirement 6.6).
RUN npm ci

# Only what `vite build` reads. `public/` matters here: it holds the placeholder
# config.js that Vite copies into dist, which is the file this image's entrypoint
# replaces — see the note in the runtime stage about which copy actually gets
# served.
COPY tsconfig.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src

RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: serve.
#
# A fresh nginx image that receives the built assets, the server template, and the
# entrypoint script. Nothing is inherited from stage one.
# ---------------------------------------------------------------------------
FROM ${NGINX_IMAGE} AS runtime

# The base image sets `USER 101`. Root is needed briefly to write outside the
# runtime user's reach — which is the whole point of the ownership below — and the
# stage ends by dropping back.
USER root

# Three things, and each of them removes a way this image could lie about its
# state:
#
#   - the stock default.conf, so a failed template render leaves nginx with no
#     server block instead of a working welcome page (see the header);
#   - the welcome page and 50x.html, so /usr/share/nginx/html contains this
#     application and nothing else;
#   - the writable runtime directory, created here with an explicit owner so the
#     entrypoint's write is the only write the runtime user ever needs.
#
# Group 0 with group-write matches how the base image prepares its own writable
# directories, and it is what lets an arbitrary `runAsUser` in group 0 — the
# pattern OpenShift enforces — write here too.
RUN rm -f /etc/nginx/conf.d/default.conf \
          /usr/share/nginx/html/index.html \
          /usr/share/nginx/html/50x.html \
 && install -d -o 101 -g 0 -m 0775 /usr/share/nginx/runtime

# Root-owned: readable by uid 101, writable by nobody (Requirement 6.3).
COPY --from=build --chown=root:root /app/dist/ /usr/share/nginx/html/

# dist carries a copy of public/config.js, and leaving it here would be a second
# config.js in the image that nginx never serves — `location = /config.js` reads
# from the runtime directory instead. Deleting it means there is exactly one
# answer to "where does the runtime configuration come from", which is worth more
# than 200 bytes of dead placeholder.
RUN rm -f /usr/share/nginx/html/config.js

# Rendered into /etc/nginx/conf.d/default.conf at start by the base image's
# 20-envsubst-on-templates.sh.
COPY --chown=root:root nginx/default.conf.template /etc/nginx/templates/default.conf.template

# Run by the base image's /docker-entrypoint.sh, which skips files without the
# execute bit — hence the explicit mode rather than whatever the host's umask left
# on the file.
COPY --chown=root:root --chmod=0755 nginx/50-publishhub-runtime-config.sh /docker-entrypoint.d/50-publishhub-runtime-config.sh

# The image's own defaults, so `docker run` with no environment produces a working
# container and a reader can see the contract without opening the entrypoint.
# Everything here is overridden per environment by the chart's ConfigMap.
#
#   API_BASE_URL           what the browser prefixes onto API paths. Relative by
#                          default, which is what makes requests same-origin.
#   API_UPSTREAM           where nginx forwards /api. `publishhub-redis` is the
#                          existing precedent for a Service name shared between
#                          compose and the chart; this is the API's equivalent.
#                          scheme://host[:port], no path — the entrypoint enforces
#                          it.
#   NGINX_ENVSUBST_FILTER  restricts envsubst to this one variable, so nginx's own
#                          `$uri` and `$host` survive the render. No trailing `$`
#                          anchor: a Dockerfile ENV value ending in `$` is a
#                          shell-expansion trap for no benefit, and the prefix is
#                          already unambiguous.
#   PUBLISHHUB_CONFIG_DIR  stated once, here, rather than hardcoded twice in the
#                          entrypoint and the nginx template.
ENV API_BASE_URL=/api \
    API_UPSTREAM=http://publishhub-api:8080 \
    NGINX_ENVSUBST_FILTER=^API_UPSTREAM \
    PUBLISHHUB_CONFIG_DIR=/usr/share/nginx/runtime

# Numerically, matching the base image, and asserted again by the chart's
# `runAsUser`/`runAsNonRoot` (task 9.2) so the Kubernetes-level guarantee does not
# depend on this line.
USER 101

# Documentation for humans and for `docker run -P`; it publishes nothing by
# itself. The Service and the probes in the Helm chart are the real contract.
EXPOSE 8080

# Restated rather than inherited, so that `docker inspect` on this image answers
# the question without a second lookup at the base, and so a base-image change to
# CMD cannot quietly change what this image runs. `daemon off;` keeps nginx in the
# foreground as PID 1.
CMD ["nginx", "-g", "daemon off;"]
