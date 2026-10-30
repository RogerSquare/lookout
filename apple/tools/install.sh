#!/bin/bash
# Debug build installed straight to the USB/Wi-Fi-connected iPhone (the watch app rides along).
#   tools/install.sh [device-id]    default: the first connected physical iPhone
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(grep -E '^(KEY_ID|ISSUER_ID|KEY_PATH)=' ~/.appstoreconnect/config)"  # macOS bash 3.2 can't source <(...)
KC=~/Library/Keychains/lookout-signing.keychain-db
security unlock-keychain -p "$(cat ~/.lookout-signing/keychain-pass)" $KC
DEV=${1:-$(xcrun devicectl list devices 2>/dev/null | awk '/physical/ && /iPhone/ && /connected/ {for (i=1;i<=NF;i++) if ($i ~ /^[0-9A-F]{8}-[0-9A-F]{16}$/) print $i; exit}')}
[ -n "$DEV" ] || { echo "no connected iPhone"; exit 1; }
xcodebuild -project Lookout.xcodeproj -scheme Lookout -configuration Debug -destination "id=$DEV" -derivedDataPath build/dd \
  -allowProvisioningUpdates -authenticationKeyPath "$KEY_PATH" -authenticationKeyID "$KEY_ID" \
  -authenticationKeyIssuerID "$ISSUER_ID" OTHER_CODE_SIGN_FLAGS="--keychain $KC" build 2>&1 | grep -E "error|BUILD" | sort -u
xcrun devicectl device install app --device "$DEV" build/dd/Build/Products/Debug-iphoneos/Lookout.app | tail -2
xcrun devicectl device process launch --device "$DEV" dev.rogersquare.lookout | tail -1
