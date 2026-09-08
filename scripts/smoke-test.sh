#!/usr/bin/env bash
#
# Unified-repository smoke test: prove the four relocated stacks coexist.
#
# This is NOT a replacement for the component test suites, and it deliberately
# proves nothing about media. It changes no application state: it creates no
# camera, no event and no recording, and it makes no mutating HTTP request.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES_DIR="$REPO_ROOT/services"

# directory | compose service | container port | expected project | readiness substrings
STACKS=(
  "rtsp-simulator|simulator|8080|genea-simulator|\"status\":\"ok\";\"ffmpeg\":true;\"ffprobe\":true;\"database\":true"
  "vms|vms|8090|genea-vms|\"status\":\"ok\";\"database\":\"ok\";\"reachable\":true"
  "video-analytics|analytics|8100|genea-analytics|\"status\":\"ok\";\"database\":\"ok\";\"event_storage\":\"ok\";\"detector\":\"ok\""
  "semantic-search|semantic-search|8200|genea-semantic-search|\"search\":\"ok\";\"model\":\"ok\";\"database\":\"ok\";\"index\":\"ok\""
)

pass=0
fail=0
ok()  { printf 'PASS  %s\n' "$*"; pass=$((pass + 1)); }
no()  { printf 'FAIL  %s\n' "$*" >&2; fail=$((fail + 1)); }

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

printf '== Structure and Compose identity\n'

[ ! -e "$REPO_ROOT/docker-compose.yml" ] && [ ! -e "$REPO_ROOT/compose.yml" ] \
  && ok "no root Compose file (each stack stays independent)" \
  || no "a root Compose file exists; the four stacks must stay independent"

[ ! -d "$REPO_ROOT/app" ] \
  && ok "no root application package" \
  || no "a root app/ directory exists"

seen=""
for entry in "${STACKS[@]}"; do
  IFS='|' read -r dir svc port expected checks <<<"$entry"
  path="$SERVICES_DIR/$dir"

  if [ -f "$path/docker-compose.yml" ]; then
    ok "services/$dir/docker-compose.yml exists"
  else
    no "services/$dir/docker-compose.yml is missing"
    continue
  fi

  if ( cd "$path" && docker compose config --quiet ) 2>/dev/null; then
    ok "services/$dir Compose parses"
  else
    no "services/$dir Compose does not parse"
    continue
  fi

  actual="$( cd "$path" && project_name )"
  if [ "$actual" = "$expected" ]; then
    ok "services/$dir project name is $expected"
  else
    no "services/$dir project name is '$actual', expected '$expected'"
  fi

  case " $seen " in
    *" $actual "*) no "project name '$actual' is shared by more than one stack" ;;
    *)             seen="$seen $actual" ;;
  esac
done

printf '\n== Running containers and health\n'

for entry in "${STACKS[@]}"; do
  IFS='|' read -r dir svc port expected checks <<<"$entry"
  path="$SERVICES_DIR/$dir"
  [ -f "$path/docker-compose.yml" ] || continue

  running="$( cd "$path" && docker compose ps --services --filter status=running 2>/dev/null || true )"
  if printf '%s\n' "$running" | grep -qx "$svc"; then
    ok "services/$dir service '$svc' is running"
  else
    no "services/$dir service '$svc' is not running"
    continue
  fi

  bound="$( cd "$path" && docker compose port "$svc" "$port" 2>/dev/null || true )"
  host_port="${bound##*:}"
  [ -n "$host_port" ] || host_port="$port"

  code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
            "http://localhost:$host_port/health" 2>/dev/null || echo 000)"
  body="$(curl --silent --max-time 10 "http://localhost:$host_port/health" 2>/dev/null || true)"

  if [ "$code" = "200" ]; then
    ok "services/$dir /health returned HTTP 200 on :$host_port"
  else
    no "services/$dir /health returned HTTP $code on :$host_port"
    continue
  fi

  IFS=';' read -r -a wanted <<<"$checks"
  for want in "${wanted[@]}"; do
    case "$body" in
      *"$want"*) ok "services/$dir health reports $want" ;;
      *)         no "services/$dir health is missing $want  (body: $body)" ;;
    esac
  done

  if [ "$dir" = "semantic-search" ]; then
    case "$body" in
      *'"upstream":"unavailable"'*)
        printf 'NOTE  semantic-search upstream Component 4 is unavailable.\n'
        printf '      Local search is still ready, so this is not a failure.\n'
        ;;
      *'"upstream":"available"'*)
        ok "services/semantic-search upstream Component 4 is available"
        ;;
    esac
  fi
done

printf '\n== Result\n'
printf 'passed %s, failed %s\n' "$pass" "$fail"
printf '\n'
printf 'This smoke test proves only that the four stacks coexist, keep distinct\n'
printf 'Compose identities and are locally ready. It does NOT prove media flow,\n'
printf 'browser playback, recording, event creation or search ranking. Those\n'
printf 'require the component suites and the manual acceptance walkthrough.\n'

[ "$fail" -eq 0 ] || exit 1
