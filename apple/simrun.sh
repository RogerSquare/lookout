#!/bin/bash
# simrun.sh <scheme> <sim-id> <platform> <bundle-id> <app-name>: build, (re)install and launch in a simulator
cd "$(dirname "$0")"
xcodebuild -project Lookout.xcodeproj -scheme "$1" -destination "id=$2" -derivedDataPath build CODE_SIGNING_ALLOWED=NO build 2>&1 | grep -E "error|BUILD" | sort -u
xcrun simctl terminate "$2" "$4" 2>/dev/null
xcrun simctl install "$2" "build/Build/Products/Debug-$3/$5.app" && xcrun simctl launch "$2" "$4" >/dev/null
