#!/bin/sh
# Generate the frontend's runtime configuration at container start
# (Requirement 4.7).
#
# The frontend is a static bundle, so anything environment-specific has to arrive
# after the build. index.html loads /config.js as a classic script before the
# module bundle, and that file sets `window.__PUBLISHHUB_CONFIG__`, which
# src/config/runtime-config.ts reads and validates. This script writes that file
# from the environment, which is what lets one image serve every environment with
# no rebuild.
#
# It is installed as /docker-entrypoint.d/50-publishhub-runtime-config.sh and run
# by the base image's /docker-entrypoint.sh, which executes every *.sh in that
# directory in sort order and then `exec`s nginx. Reusing that mechanism rather
# than replacing ENTRYPOINT keeps the base image's own steps (template rendering,
# worker-process tuning) and keeps nginx as PID 1, so signals still reach it.
#
# The number is 50 because the image already ships 10-, 15-, 20- and 30- scripts;
# running last means the nginx config has already been rendered by the time this
# validates the values it was rendered from.
#
# Failure is fatal on purpose. /docker-entrypoint.sh runs under `set -e`, so a
# non-zero exit here stops the container before nginx starts. A frontend pointed
# at a nonexistent API by a typo should fail visibly at rollout, not serve a page
# whose every request fails in the browser.

set -eu

ME=$(basename "$0")

# Where the generated file goes. It is a directory the runtime user can write,
# separate from the root-owned asset directory, and nginx serves `/config.js` from
# it via a `location = /config.js` with its own `root` (Requirement 6.3). Kept as
# an environment variable so the Dockerfile states the path once and this script
# does not hardcode a second copy of it.
CONFIG_DIR="${PUBLISHHUB_CONFIG_DIR:-/usr/share/nginx/runtime}"

# Both defaults match the design's configuration reference and the Dockerfile's
# ENV, so this script behaves the same whether or not the image set them.
API_BASE_URL="${API_BASE_URL:-/api}"
API_UPSTREAM="${API_UPSTREAM:-http://publishhub-api:8080}"

# Same convention as the base image's scripts: NGINX_ENTRYPOINT_QUIET_LOGS
# silences the startup chatter without silencing errors.
log() {
    if [ -z "${NGINX_ENTRYPOINT_QUIET_LOGS:-}" ]; then
        echo "$ME: $*"
    fi
}

fail() {
    echo "$ME: ERROR: $*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Validate API_BASE_URL.
#
# This value is interpolated into a JavaScript string literal that every browser
# session executes, so it is validated rather than escaped. Validation is the
# stronger of the two here: the accepted character set contains no quote,
# backslash, angle bracket, brace or whitespace, so an accepted value cannot
# terminate the string it lands in, and there is no escaping logic to get subtly
# wrong. Anything outside the set is a misconfiguration, not a value worth
# smuggling through.
#
# The rules mirror normalizeBaseUrl in src/config/runtime-config.ts, so a value
# accepted here is a value the app accepts, and the frontend's fallback stays a
# defence in depth rather than the thing that routinely saves us.
# ---------------------------------------------------------------------------
[ -n "$API_BASE_URL" ] || fail "API_BASE_URL is set but empty; unset it to take the default /api"

case "$API_BASE_URL" in
    //*)
        # `//host/path` is a protocol-relative URL, not a path. The app rejects it
        # too: a runtime config should be explicit about its scheme.
        fail "API_BASE_URL must not be protocol-relative (got '$API_BASE_URL'); use /api or https://host"
        ;;
    /* | http://* | https://*) ;;
    *)
        # Also the shape an unsubstituted placeholder has, e.g. a chart value that
        # never got rendered.
        fail "API_BASE_URL must be a path starting with / or an http(s) URL (got '$API_BASE_URL')"
        ;;
esac

if printf '%s' "$API_BASE_URL" | LC_ALL=C grep -q '[^A-Za-z0-9._~:/?#@!$&()*+,;=%-]'; then
    fail "API_BASE_URL contains characters that cannot appear in a URL (got '$API_BASE_URL')"
fi

# ---------------------------------------------------------------------------
# Validate API_UPSTREAM.
#
# nginx has already been handed this value by the template render, so the check is
# about the error message rather than about preventing the write: `proxy_pass`
# with a bad URL fails with a parse error that names a line in a generated file
# and never mentions the environment variable behind it.
#
# A path on the target changes proxy_pass semantics from "forward the request URI
# unchanged" to "replace the location prefix with this path", which silently 404s
# every API call. A bare trailing slash is that same case with an empty path, so
# both are rejected.
# ---------------------------------------------------------------------------
case "$API_UPSTREAM" in
    http://* | https://*) ;;
    *) fail "API_UPSTREAM must start with http:// or https:// (got '$API_UPSTREAM')" ;;
esac

case "${API_UPSTREAM#*://}" in
    "") fail "API_UPSTREAM has no host (got '$API_UPSTREAM')" ;;
    */*) fail "API_UPSTREAM must be scheme://host[:port] with no path or trailing slash (got '$API_UPSTREAM')" ;;
esac

# ---------------------------------------------------------------------------
# Write the file.
# ---------------------------------------------------------------------------
[ -d "$CONFIG_DIR" ] || fail "$CONFIG_DIR does not exist; the image creates it, so it was probably shadowed by an empty mount"
[ -w "$CONFIG_DIR" ] || fail "$CONFIG_DIR is not writable by uid $(id -u); a volume mounted there needs to be writable by the runtime user (set fsGroup, or mount an emptyDir)"

target="$CONFIG_DIR/config.js"
tmp="$target.$$.tmp"

# Written to a temporary file and renamed, so /config.js is never a half-written
# file. Nothing is serving it yet at this point in startup, but a restarted
# container with a persistent mount is a different story, and `mv` within one
# directory is atomic.
cat > "$tmp" <<EOF
// Generated by $ME at container start. Rewritten on every start; edits are lost.
//
// Read by src/config/runtime-config.ts through window.__PUBLISHHUB_CONFIG__.
window.__PUBLISHHUB_CONFIG__ = {
  apiBaseUrl: "$API_BASE_URL",
};
EOF

# World-readable: nginx serves it, and the worker processes may drop to a
# different user than the one that ran this script.
chmod 0644 "$tmp"
mv "$tmp" "$target"

log "wrote $target with apiBaseUrl=$API_BASE_URL"
log "/api/ is proxied to $API_UPSTREAM"
