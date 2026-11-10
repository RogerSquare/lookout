#!/bin/bash
# Build, sign and upload Lookout (iPhone + embedded watch app) to TestFlight, headless.
#   tools/ship.sh            archive + upload; build number = UTC timestamp
#   tools/ship.sh --no-upload   archive + export an .ipa only
set -euo pipefail
cd "$(dirname "$0")/.."
eval "$(grep -E '^(KEY_ID|ISSUER_ID|KEY_PATH)=' ~/.appstoreconnect/config)"  # macOS bash 3.2 can't source <(...)
KC=~/Library/Keychains/lookout-signing.keychain-db
security unlock-keychain -p "$(cat ~/.lookout-signing/keychain-pass)" $KC

BUILD=$(date -u +%Y%m%d%H%M)
AUTH=(-allowProvisioningUpdates -authenticationKeyPath "$KEY_PATH" -authenticationKeyID "$KEY_ID"
      -authenticationKeyIssuerID "$ISSUER_ID")
rm -rf build/Lookout.xcarchive build/export

xcodebuild -project Lookout.xcodeproj -scheme Lookout -configuration Release -destination "generic/platform=iOS" \
  -archivePath build/Lookout.xcarchive -derivedDataPath build/dd "${AUTH[@]}" \
  CURRENT_PROJECT_VERSION=$BUILD OTHER_CODE_SIGN_FLAGS="--keychain $KC" archive 2>&1 \
  | grep -E "error|warning: .*(sign|provision)|ARCHIVE" | sort -u

# Export signs manually with our distribution cert + App Store profiles (made by asc.py profile):
# an App Manager API key can't use Xcode's cloud signing
PROFILES=~/Library/Developer/Xcode/UserData/Provisioning\ Profiles
mkdir -p "$PROFILES"
PLIST_PROFILES=""
for id in dev.rogersquare.lookout dev.rogersquare.lookout.watchkitapp dev.rogersquare.lookout.watchkitapp.widget; do
  f=~/.lookout-signing/profiles/$id.appstore.mobileprovision
  uuid=$(security cms -D -i "$f" | plutil -extract UUID raw -o - -)
  cp "$f" "$PROFILES/$uuid.mobileprovision"
  PLIST_PROFILES="$PLIST_PROFILES<key>$id</key><string>$uuid</string>"
done
DEST=$([ "${1:-}" = "--no-upload" ] && echo export || echo upload)
cat > build/ExportOptions.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>$DEST</string>
  <key>teamID</key><string>NKQFLG7YYR</string>
  <key>signingStyle</key><string>manual</string>
  <key>signingCertificate</key><string>Apple Distribution</string>
  <key>provisioningProfiles</key><dict>$PLIST_PROFILES</dict>
  <key>uploadSymbols</key><true/>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
EOF
xcodebuild -exportArchive -archivePath build/Lookout.xcarchive -exportPath build/export \
  -exportOptionsPlist build/ExportOptions.plist "${AUTH[@]}" 2>&1 | grep -E "error|Upload|EXPORT|uploaded" | sort -u
echo "build $BUILD"
