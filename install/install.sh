#!/bin/sh
# Lookout Hub installer for Linux, NAS and Raspberry Pi (64-bit), and macOS with Docker Desktop.
#   curl -fsSL https://github.com/RogerSquare/lookout/releases/latest/download/install.sh | sh
# Installs into ./lookout-hub (or $LOOKOUT_DIR), starts the hub, and prints the setup link.
set -eu

DIR="${LOOKOUT_DIR:-$PWD/lookout-hub}"
BASE="https://github.com/RogerSquare/lookout/releases/latest/download"

say() { printf '%s\n' "$*"; }
fail() { say "Lookout: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker isn't installed. Get it from https://docs.docker.com/get-docker/ and run this again."
docker info >/dev/null 2>&1 || fail "Docker isn't running, or this user can't use it. Start Docker, or run this with sudo."
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
else fail "Docker Compose is missing. Install the Docker Compose plugin and run this again."; fi

case "$(uname -m)" in
  x86_64|amd64|aarch64|arm64) ;;
  *) fail "This computer ($(uname -m)) isn't supported. Lookout Hub needs a 64-bit Intel/AMD or ARM processor." ;;
esac

mkdir -p "$DIR/data"
cd "$DIR"

if [ "$(uname -s)" = "Darwin" ]; then
  # Docker Desktop: no host networking, so tell the hub this Mac's address
  FILE=compose.desktop.yml
  IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
  [ -n "$IP" ] || fail "Couldn't find this Mac's network address. Connect to your home network and try again."
  printf 'LOOKOUT_PUBLIC_HOST=%s\n' "$IP" > .env
else
  FILE=compose.yml
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi

say "Downloading Lookout Hub into $DIR ..."
curl -fsSL -o "$FILE" "$BASE/$FILE"
$COMPOSE -f "$FILE" pull
$COMPOSE -f "$FILE" up -d

say "Starting (the first start tests this computer's speed, about a minute) ..."
i=0
while [ $i -lt 90 ]; do
  if curl -fs "http://127.0.0.1:8765/healthz" >/dev/null 2>&1; then break; fi
  i=$((i + 1)); sleep 2
done

CODE=$(docker exec lookout-hub cat /data/setup-code 2>/dev/null | tr -d '[:space:]' || true)
say ""
if [ -n "$CODE" ]; then
  say "Lookout Hub is running. Open this link to finish setting it up:"
  say "  http://${IP:-<this computer>}:8765/?setup=$CODE"
  say "(or run 'docker logs lookout-hub' to see a QR code you can scan with your phone)"
else
  say "Lookout Hub is running. Open http://${IP:-<this computer>}:8765 to finish setting it up."
fi
say "To update later:  cd $DIR && $COMPOSE -f $FILE pull && $COMPOSE -f $FILE up -d"
