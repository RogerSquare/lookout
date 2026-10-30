# Lookout for Apple Watch and iPhone

SwiftUI apps that show the cameras on a Lookout Hub (`../hub`) live, with person alerts.

- **iPhone:** pairs with the hub (scan the setup page's QR code, or pick a hub found on the network
  and type its 6-digit code), every camera live in a list, settings for alerts and the watch, recent
  visitors, optional Face ID lock. Pairs the watch for you.
- **Watch:** one full-screen page per camera; swipe between them, the Digital Crown zooms, the edge
  turns green while someone's in view. Person alerts show the detection frame. Complication for the
  door camera.

## Security

Each device makes a P-256 key in the Secure Enclave when it pairs (`Shared/HubLink.swift`) and
signs every API request with it. At home the API is HTTPS to the hub's own certificate, pinned to
the fingerprint from the QR code; away it's the hub's public HTTPS address. Players get URLs with
10-minute media tokens. The pairing and the key are in the keychain (shared by the watch app and
its complication through the `LookoutKeychainGroup` access group), readable after first unlock,
never synced or backed up.

## Build

Needs Xcode 26+. Open `Lookout.xcodeproj`, set your team and bundle ids if you're not the
maintainer, pick the iPhone, Run. The watch app is embedded in the iPhone app and installs with it.

Simulator: `./simrun.sh LookoutWatch <sim-id> watchsimulator dev.rogersquare.lookout.watchkitapp LookoutWatch`
(or `Lookout <sim-id> iphonesimulator dev.rogersquare.lookout Lookout`).

## Headless signing and TestFlight (driven over SSH)

`ssh macbook` can build, sign and ship without Xcode's UI:

- `~/.appstoreconnect/config` + `AuthKey_<id>.p8`: App Store Connect API key (App Manager role).
- `~/.lookout-signing/`: a dedicated keychain (`lookout-signing.keychain-db`, password in
  `keychain-pass`) with a development and a distribution certificate created through the API,
  plus App Store profiles in `profiles/`. Set up once with `tools/setup-signing.sh`.
- `tools/ship.sh`: archive, export with the distribution profiles, upload to TestFlight.
  The internal TestFlight group "Me" gets every build (`tools/asc.py testflight <bundle-id>`).
- `tools/install.sh`: debug build straight to the connected iPhone.
- `tools/asc.py`: the small API client behind it (certs, profiles, devices, builds).

Debug installs onto the Apple Watch need Xcode to see the watch, which Xcode 27 doesn't do for
an original Ultra on watchOS 26; TestFlight installs it instead.
