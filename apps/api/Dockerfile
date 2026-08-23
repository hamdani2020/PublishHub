ARG NODE_IMAGE=node:24.19.0-bookworm-slim

ARG NPM_VERSION=11.6.2

FROM ${NODE_IMAGE} AS deps

WORKDIR /app

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

ARG NPM_VERSION
RUN npm install --global "npm@${NPM_VERSION}"

COPY package.json package-lock.json ./

RUN npm ci

FROM deps AS build

WORKDIR /app

COPY tsconfig.json tsconfig.build.json ./
COPY src ./src

RUN npm run build

FROM deps AS prod-deps

WORKDIR /app

RUN npm prune --omit=dev

FROM ${NODE_IMAGE} AS runtime

ENV NODE_ENV=production \
    PORT=8080 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

WORKDIR /app

COPY --from=prod-deps --chown=root:root /app/node_modules ./node_modules
COPY --from=build --chown=root:root /app/dist ./dist

COPY --chown=root:root package.json ./

USER node

EXPOSE 8080

CMD ["node", "dist/index.js"]
