ARG NODE_IMAGE=node:24.19.0-bookworm-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.30.4-alpine3.24

ARG NPM_VERSION=11.6.2

FROM ${NODE_IMAGE} AS build

WORKDIR /app

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false

ARG NPM_VERSION
RUN npm install --global "npm@${NPM_VERSION}"

COPY package.json package-lock.json ./

RUN npm ci

COPY tsconfig.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src

RUN npm run build

FROM ${NGINX_IMAGE} AS runtime

USER root

RUN rm -f /etc/nginx/conf.d/default.conf \
          /usr/share/nginx/html/index.html \
          /usr/share/nginx/html/50x.html \
 && install -d -o 101 -g 0 -m 0775 /usr/share/nginx/runtime

COPY --from=build --chown=root:root /app/dist/ /usr/share/nginx/html/

RUN rm -f /usr/share/nginx/html/config.js

COPY --chown=root:root nginx/default.conf.template /etc/nginx/templates/default.conf.template

COPY --chown=root:root --chmod=0755 nginx/50-publishhub-runtime-config.sh /docker-entrypoint.d/50-publishhub-runtime-config.sh
    
ENV API_BASE_URL=/api \
    API_UPSTREAM=http://publishhub-api:8080 \
    NGINX_ENVSUBST_FILTER=^API_UPSTREAM \
    PUBLISHHUB_CONFIG_DIR=/usr/share/nginx/runtime

USER 101

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
