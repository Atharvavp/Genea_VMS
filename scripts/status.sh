#!/usr/bin/env bash
#
# Report what each Genea stack is doing. Read-only: it starts nothing, stops
# nothing and changes no application state.
#
# It deliberately does not collapse everything into one boolean. "container
# running", "application ready" and "upstream available" are different things,
# and the last one being false is normal for Semantic Search.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES_DIR="$REPO_ROOT/services"

STACKS=(
  "rtsp-simulator|simulator|8080|genea-simulator|Simulator"
  "vms|vms|8090|genea-vms|VMS"
  "video-analytics|analytics|8100|genea-analytics|Analytics"
  "semantic-search|semantic-search|8200|genea-semantic-search|Semantic Search"
)

unreachable=0

for entry in "${STACKS[@]}"; do
  IFS='|' read -r dir svc port project label <<<"$entry"
  path="$SERVICES_DIR/$dir"

  printf '\n== %s   (services/%s, project %s)\n' "$label" "$dir" "$project"

  if [ ! -f "$path/docker-compose.yml" ]; then
    printf '  MISSING   services/%s/docker-compose.yml\n' "$dir"
    unreachable=$((unreachable + 1))
    continue
  fi

  ( cd "$path" && docker compose ps ) 2>/dev/null | sed 's/^/  /' || printf '  (compose ps unavailable)\n'

  bound="$( cd "$path" && docker compose port "$svc" "$port" 2>/dev/null || true )"
  host_port="${bound##*:}"
  if [ -z "$host_port" ]; then
    printf '  state     not running (no published port)\n'
    unreachable=$((unreachable + 1))
    continue
  fi

  code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 \
            "http://localhost:$host_port/health" 2>/dev/null || echo 000)"
  body="$(curl --silent --max-time 5 "http://localhost:$host_port/health" 2>/dev/null || true)"

  if [ "$code" = "000" ]; then
    printf '  state     container up, application NOT ANSWERING on :%s\n' "$host_port"
    unreachable=$((unreachable + 1))
    continue
  fi

  case "$body" in
    *'"status":"ok"'*)       app="application ready" ;;
    *'"status":"degraded"'*) app="application ready, DEGRADED" ;;
    *)                       app="application answering" ;;
  esac
  printf '  state     %s (HTTP %s)\n' "$app" "$code"

  case "$body" in
    *'"upstream":"available"'*)   printf '  upstream  available\n' ;;
    *'"upstream":"unavailable"'*) printf '  upstream  UNAVAILABLE (local search still works)\n' ;;
  esac
  case "$body" in
    *'"reachable":true'*)  printf '  mediamtx  reachable\n' ;;
    *'"reachable":false'*) printf '  mediamtx  UNREACHABLE\n' ;;
  esac

  printf '  health    %s\n' "$body"
  printf '  url       http://localhost:%s   docs http://localhost:%s/docs\n' "$host_port" "$host_port"
  case "$dir" in
    rtsp-simulator) printf '  media     rtsp://localhost:8554/simulator/<stream_path>\n' ;;
    vms)            printf '  media     rtsp://localhost:8555/vms_<camera-id>   WHEP http://localhost:8889   playback http://localhost:9996\n' ;;
  esac
done

printf '\n'
if [ "$unreachable" -gt 0 ]; then
  printf '%s stack(s) could not be queried.\n' "$unreachable"
  exit 1
fi
printf 'All four stacks answered.\n'
