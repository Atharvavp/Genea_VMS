#!/usr/bin/env bash
#
# Stop every Genea stack, in reverse dependency order, from its own directory.
#
# Data is preserved: this runs `docker compose down` WITHOUT -v, so every named
# volume survives. It never prunes Docker and never touches a project that is
# not one of the four services in this repository.
#
# To delete one service's data, do it explicitly and per service, e.g.
#   cd services/video-analytics && docker compose down -v
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES_DIR="$REPO_ROOT/services"

# Reverse dependency order: downstream consumers first.
STACKS=(
  "semantic-search|Semantic Search"
  "video-analytics|Analytics"
  "vms|VMS"
  "rtsp-simulator|Simulator"
)

failures=()

for entry in "${STACKS[@]}"; do
  IFS='|' read -r dir label <<<"$entry"
  path="$SERVICES_DIR/$dir"

  printf '\n== Stopping %s  (services/%s)\n' "$label" "$dir"

  if [ ! -f "$path/docker-compose.yml" ]; then
    printf '  skipped: services/%s/docker-compose.yml not found\n' "$dir"
    failures+=("$dir (missing compose file)")
    continue
  fi

  # No -v: volumes are kept. No --remove-orphans: it must never reach a peer.
  if ( cd "$path" && docker compose down ); then
    printf '  stopped, volumes kept\n'
  else
    printf '  FAILED to stop cleanly\n' >&2
    failures+=("$dir")
  fi
done

printf '\n'
if [ ${#failures[@]} -gt 0 ]; then
  printf 'These stacks did not stop cleanly: %s\n' "${failures[*]}" >&2
  exit 1
fi
printf 'All four stacks stopped. Named volumes were kept.\n'
