# PublishHub API — production image (Requirements 6.2, 6.3, 6.6, 2.8).
#
# Four stages, and each boundary exists to keep something out of the layer that
# ships:
#
#   deps       full dependency tree, dev included — the only stage that can compile
#   build      `tsc` runs here, so TypeScript never reaches the runtime image
#   prod-deps  the same tree with dev dependencies pruned away
#   runtime    node + node_modules + dist, and nothing else
#
# What that buys is Requirement 6.2 concretely rather than nominally: the final
# image has no TypeScript compiler, no eslint, no vitest, no test sources, and no
# package manager cache. It also means the thing that got type-checked is the
# thing that gets deployed, because `dist` is copied from the stage that produced
# it rather than rebuilt or trusted from the host.
#
# Three properties are load-bearing at deploy time and easy to break by accident:
#
# 1. **Pinned base image.** `node:24.19.0-bookworm-slim`, not `node:24-slim` and
#    not `node:slim` (Requirement 6.6). It is the same tag docker-compose.yaml
#    uses for the inner loop, so the interpreter that runs your tests is the
#    interpreter that runs in the cluster. Dependency versions are pinned in
#    package.json with no `^` or `~`, and `npm ci` installs from the lockfile, so
#    the tree is reproducible without this file having to restate a version.
#
#    Node 24 rather than the Node 20 the design document names, because
#    `dd-trace@6.10.0` declares `engines: { node: ">=22" }` — the tracer
#    Requirement 14 depends on does not support Node 20, and an image that breaks
#    the moment observability is switched on is not a working image.
#    package.json's `engines` and docker-compose.yaml were moved to the same
#    version so the three agree.
#
#    npm is pinned too, and that one is not decoration. `npm ci` refuses a
#    lockfile it would not have written itself, and which packages npm considers
#    part of the tree changes between npm releases: npm 11.17.0, the version
#    node:24.19.0 happens to ship, wants `@openfeature/server-sdk`,
#    `@openfeature/core`, and two `@emnapi/*` packages hoisted to the top level,
#    where the committed lockfile has them absent or nested. npm 10.8.2 rejects
#    the same lockfile for the same four packages. So the resolver is part of the
#    toolchain that has to be pinned, exactly like the interpreter — otherwise a
#    base-image patch bump silently breaks the build with a message that blames
#    the lockfile.
# 2. **Non-root.** The `node` user (uid 1000) ships with the base image, and every
#    application file is owned by root, so the runtime user can read the app and
#    write nowhere in it (Requirement 6.3). Nothing here needs a writable
#    filesystem: logs go to stdout, post state lives in Redis, and the process
#    holds no on-disk state, so this image runs unchanged under
#    `readOnlyRootFilesystem: true` with a `/tmp` emptyDir.
# 3. **Signals reach the process.** `CMD` is exec form with `node` as the
#    argv[0], so `node` is PID 1 and Kubernetes' `SIGTERM` is delivered straight
#    to it. There is deliberately no `sh -c` wrapper — a shell in front of the
#    process is the classic way to lose a signal, and losing it here would mean a
#    rollout killing in-flight publish requests instead of draining them
#    (Requirement 2.8). The handler that receives it is
#    `installShutdownHandlers` in src/http/shutdown.ts; because Node installs no
#    default `SIGTERM` handler, a PID 1 node process with no handler would ignore
#    the signal entirely, and that handler is what makes PID 1 safe here.
#
# No init shim (tini, dumb-init) on purpose. Its two jobs are forwarding signals
# to a child and reaping orphaned zombies; this process has no children and
# receives the signal directly, so an init would add a package to scan and patch
# in exchange for nothing. The `init: true` in docker-compose.yaml is a different
# case — there the command is `sh -c npm install && …`, which does have children.
#
# Build from this directory:
#   docker build -t publishhub-api:dev apps/api
# `make apps-build` (spec task 7.3) does this for all three services and tags each
# with both `latest` and the Git commit SHA.

# Declared once, used by every stage, so an upgrade is a one-line change and the
# stages can never drift onto different Node versions. Overridable for a
# multi-arch or patch-level rebuild without editing the file.
ARG NODE_IMAGE=node:24.19.0-bookworm-slim

# The npm that wrote package-lock.json, and therefore the only npm guaranteed to
# be able to read it with `npm ci`. See the note above; bump this together with
# the lockfile, in a commit whose diff shows both.
#
# If this build starts failing with "Missing: … from lock file", the lockfile was
# rewritten by a different npm — `npm install` does that, including the one the
# api service runs at start in docker-compose.yaml — and reconciling the two is
# the fix, not `npm install` inside this Dockerfile.
ARG NPM_VERSION=11.6.2

# ---------------------------------------------------------------------------
# Stage 1: dependencies.
#
# Only the manifest and the lockfile are copied, so this layer is cached until a
# dependency actually changes — editing a source file does not re-resolve the
# tree. `npm ci` rather than `npm install`: it installs exactly what
# package-lock.json records and fails if the lockfile and package.json disagree,
# which is what makes the build reproducible (Requirement 6.6).
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS deps

WORKDIR /app

# Noise suppression, not behavior. The audit and funding steps reach the network
# for information nobody reads in a build log, and the update notifier prints a
# box telling a container to upgrade its own npm. Set as configuration rather than
# as flags so they apply to every npm invocation in this stage and its children.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

# Its own layer, above the `COPY`, so the pinned npm is fetched once and reused by
# every later build. `--global` installs into /usr/local, which only this stage and
# its children see — the runtime stage is a fresh base image and keeps the npm it
# shipped with, which it never runs.
ARG NPM_VERSION
RUN npm install --global "npm@${NPM_VERSION}"

COPY package.json package-lock.json ./

RUN npm ci

# ---------------------------------------------------------------------------
# Stage 2: build.
#
# `tsc --project tsconfig.build.json`, which excludes `src/**/*.test.ts` and
# `src/**/testing/**`, so the emitted `dist` contains production code only. The
# test sources are also kept out of the context by .dockerignore; belt and braces,
# because the cost is zero and a fake queue client in a production image is the
# kind of thing nobody notices until it matters.
# ---------------------------------------------------------------------------
FROM deps AS build

WORKDIR /app

COPY tsconfig.json tsconfig.build.json ./
COPY src ./src

RUN npm run build

# ---------------------------------------------------------------------------
# Stage 3: production dependencies.
#
# `npm prune --omit=dev` on the tree stage 1 already installed, rather than a
# second `npm ci --omit=dev`. Same result, no second resolution, and — the reason
# that matters — the packages that ship are byte-identical to the ones the build
# compiled against, because they are the same install with entries removed.
#
# `dd-trace` is a runtime dependency and survives the prune by design: it is
# `require`d only when `OBSERVABILITY_ENABLED` is true (see
# src/observability/dd-trace-loader.ts), so it costs image size and nothing else
# when Datadog is off (Requirement 14.6).
# ---------------------------------------------------------------------------
FROM deps AS prod-deps

WORKDIR /app

RUN npm prune --omit=dev

# ---------------------------------------------------------------------------
# Stage 4: runtime.
#
# A fresh base image, not a continuation of the build stages, so nothing from
# them can be inherited implicitly — this layer contains exactly the three things
# copied into it.
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS runtime

# `NODE_ENV=production` is what the ecosystem reads for express's view caching and
# error verbosity, and what src/config/config.ts validates: with it set, the
# development-only `CORS_ORIGINS=*` becomes a startup failure rather than a
# silently wide-open API (Requirement 2.9). Every other configuration value comes
# from the environment at run time — the chart's ConfigMap, or docker-compose —
# so one image serves every environment. `PORT` is stated because a reader of the
# image should not have to open the config loader to learn which port to expose.
ENV NODE_ENV=production \
    PORT=8080 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

WORKDIR /app

# Owned by root, run as `node`: the application is readable and executable by the
# runtime user and writable by nobody (Requirement 6.3). `--chown` is stated
# rather than left implicit so that intent survives a future edit.
COPY --from=prod-deps --chown=root:root /app/node_modules ./node_modules
COPY --from=build --chown=root:root /app/dist ./dist

# Required, not incidental. package.json's `"type": "module"` is what tells Node
# to load the emitted `.js` files as ES modules; without it every `import` in
# `dist` fails at startup.
COPY --chown=root:root package.json ./

# uid 1000, present in the official Node images. Referenced by name here and
# asserted numerically by the chart's `runAsUser`/`runAsNonRoot` (task 9.2),
# which is what makes the Kubernetes-level guarantee independent of this line.
USER node

# Documentation for humans and for `docker run -P`; it publishes nothing by
# itself. The Service and the probes in the Helm chart are the real contract.
EXPOSE 8080

# Exec form. See the signal note in the header — this is the line that decides
# whether `SIGTERM` reaches the shutdown handler or a shell that swallows it.
CMD ["node", "dist/index.js"]
