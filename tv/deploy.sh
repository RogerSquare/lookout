#!/bin/sh
# Build the release APK and install it on the Nebula projector over adb.
# The app restarts its alert service by itself after an update (MY_PACKAGE_REPLACED).
# First install only: grants "Display over other apps", which the popup needs.
set -e
DEVICE=${DEVICE:-10.0.0.50:5555}
ADB=${ADB:-"$LOCALAPPDATA/Microsoft/WinGet/Packages/Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe/platform-tools/adb.exe"}
cd "$(dirname "$0")"
./gradlew -q :app:testDebugUnitTest :app:assembleRelease
"$ADB" connect "$DEVICE" >/dev/null
"$ADB" -s "$DEVICE" install -r app/build/outputs/apk/release/app-release.apk
"$ADB" -s "$DEVICE" shell appops set dev.rogersquare.lookout SYSTEM_ALERT_WINDOW allow
echo "Installed. Logs: adb -s $DEVICE shell cat /sdcard/Android/data/dev.rogersquare.lookout/files/events.log"
