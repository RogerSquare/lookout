#!/bin/bash
# demoshots.sh <sim-udid> <out-dir> <cameras> <name>:<launch args> ...
# Installs the Debug build (run simrun.sh or xcodebuild first) and screenshots the app with demo
# cameras (no hub needed), once per name:args pair, e.g.
#   tools/demoshots.sh $SIM /tmp/lk 4 list:"-homeLayout list" grid:"-homeLayout grid"
set -u
cd "$(dirname "$0")/.."
U=$1; OUT=$2; N=$3; shift 3
APP=dev.rogersquare.lookout
mkdir -p "$OUT"
xcrun simctl boot "$U" 2>/dev/null; xcrun simctl bootstatus "$U" >/dev/null
xcrun simctl ui "$U" appearance dark
xcrun simctl install "$U" build/Build/Products/Debug-iphonesimulator/Lookout.app
for pair in "$@"; do
  name=${pair%%:*}; args=${pair#*:}
  xcrun simctl terminate "$U" $APP 2>/dev/null
  # shellcheck disable=SC2086
  xcrun simctl launch "$U" $APP -LookoutDemoCameras "$N" $args >/dev/null
  sleep 4
  xcrun simctl io "$U" screenshot "$OUT/$name.png" >/dev/null 2>&1 && sips -Z 900 "$OUT/$name.png" >/dev/null
  echo "$OUT/$name.png"
done
xcrun simctl terminate "$U" $APP 2>/dev/null
