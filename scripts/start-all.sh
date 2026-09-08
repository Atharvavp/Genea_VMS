#!/usr/bin/env bash
#
# Start every Genea stack, in dependency order, from its own directory.
#
# This is a convenience wrapper around four independent Compose projects. It is
# never required: each service can always be run on its own with
# `cd services/<name> && docker compose up -d --build`.
#
# It never deletes a volume, never prunes Docker, never writes a .env for you,
# and never stops a container it did not start.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES_DIR="$REPO_ROOT/services"

# directory | compose service | container port | expected project | readiness budget (s)
STACKS=(
  "rtsp-simulator|simulator|8080|genea-simulator|60"
  "vms|vms|8090|genea-vms|60"
  "video-analytics|analytics|8100|genea-analytics|180"
  "semantic-search|semantic-search|8200|genea-semantic-search|180"
)

log()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The project name Compose will actually use, read from its normalised config.
# Compose emits the top-level "name" first, so the first match is the project.
project_name() {
  docker compose config --format json 2>/dev/null | awk '
    /"name"[[:space:]]*:/ {
      if (match($0, /"name"[[:space:]]*:[[:space:]]*"[^"]*"/)) {
        s = substr($0, RSTART, RLENGTH)
        sub(/.*:[[:space:]]*"/, "", s)
        sub(/"$/, "", s)
        print s
        exit
      }
    }'
}

step "Preflight"

command -v docker >/dev/null 2>&1 || fail "docker is not on PATH"
docker compose version >/dev/null 2>&1 || fail "docker compose (v2) is not available"
command -v curl >/dev/null 2>&1 || fail "curl is not on PATH"
docker info >/dev/null 2>&1 || fail "the Docker daemon is not reachable; start Docker and retry"

seen_projects=""
for entry in "${STACKS[@]}"; do
  IFS='|' read -r dir svc port expected budget <<<"$entry"
  path="$SERVICES_DIR/$dir"
  [ -d "$path" ] || fail "missing service directory: services/$dir"
  [ -f "$path/docker-compose.yml" ] || fail "missing services/$dir/docker-compose.yml"

  # Parsing here means a missing required variable fails before anything starts.
  if ! ( cd "$path" && docker compose config --quiet ) 2>/dev/null; then
    if [ "$dir" = "video-analytics" ] && [ ! -f "$path/.env" ]; then
      fail "services/video-analytics needs its signing key. Run:
    cp services/video-analytics/.env.example services/video-analytics/.env
  This script will not create or overwrite a .env for you."
    fi
    ( cd "$path" && docker compose config --quiet ) || true
    fail "services/$dir/docker-compose.yml does not parse (see the message above)"
  fi

  actual="$( cd "$path" && project_name )"
  [ "$actual" = "$expected" ] || fail "services/$dir declares Compose project '$actual', expected '$expected'"
  case " $seen_projects " in
    *" $actual "*) fail "Compose project name '$actual' is used by more than one stack" ;;
  esac
  seen_projects="$seen_projects $actual"
  log "ok  services/$dir  project=$actual"
done
log "ok  four distinct Compose projects"

started=()
for entry in "${STACKS[@]}"; do
  IFS='|' read -r dir svc port expected budget <<<"$entry"
  path="$SERVICES_DIR/$dir"

  step "Starting services/$dir  (project $expected)"
  if ! ( cd "$path" && docker compose up -d --build ); then
    printf '\n' >&2
    printf 'error: services/%s failed to start.\n' "$dir" >&2
    if [ ${#started[@]} -gt 0 ]; then
      printf 'Already-running stacks were left up on purpose so you can diagnose: %s\n' "${started[*]}" >&2
      printf 'Stop everything with ./scripts/stop-all.sh when you are done.\n' >&2
    fi
    exit 1
  fi

  # Ask Compose which host port it actually bound, rather than assuming.
  bound="$( cd "$path" && docker compose port "$svc" "$port" 2>/dev/null || true )"
  host_port="${bound##*:}"
  [ -n "$host_port" ] || host_port="$port"

  printf 'waiting for http://localhost:%s/health (up to %ss)' "$host_port" "$budget"
  waited=0
  ready=0
  while [ "$waited" -lt "$budget" ]; do
    if body="$(curl --fail --silent --show-error --max-time 5 "http://localhost:$host_port/health" 2>/dev/null)"; then
      ready=1
      break
    fi
    printf '.'
    sleep 3
    waited=$((waited + 3))
  done
  printf '\n'

  if [ "$ready" -ne 1 ]; then
    printf 'error: services/%s did not become ready within %ss.\n' "$dir" "$budget" >&2
    printf '  logs: (cd services/%s && docker compose logs --tail 50)\n' "$dir" >&2
    if [ ${#started[@]} -gt 0 ]; then
      printf 'Already-running stacks were left up on purpose: %s\n' "${started[*]}" >&2
    fi
    exit 1
  fi

  started+=("$dir")
  case "$dir" in
    rtsp-simulator)
      log "ready  Simulator   http://localhost:$host_port   docs /docs"
      log "       RTSP        rtsp://localhost:8554/simulator/<stream_path>"
      ;;
    vms)
      log "ready  VMS         http://localhost:$host_port   docs /docs"
      log "       RTSP out    rtsp://localhost:8555/vms_<camera-id>"
      log "       WebRTC      http://localhost:8889   ICE 8189/udp   playback 9996"
      ;;
    video-analytics)
      log "ready  Analytics   http://localhost:$host_port   docs /docs"
      ;;
    semantic-search)
      log "ready  Search      http://localhost:$host_port   docs /docs"
      case "$body" in
        *'"upstream":"unavailable"'*)
          log "       note: upstream Component 4 is unavailable; local search still works."
          ;;
      esac
      ;;
  esac
done

step "All four stacks are up"
log "Check them any time with ./scripts/status.sh"
log "Stop them (keeping all data) with ./scripts/stop-all.sh"
