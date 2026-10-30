#!/bin/bash
# One-time headless signing setup on the Mac: a dedicated keychain (password in
# ~/.lookout-signing/keychain-pass) holding a development and a distribution certificate created
# through the App Store Connect API, usable by codesign over SSH without the login keychain.
# Needs ~/.appstoreconnect/config (see asc.py). Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
S=~/.lookout-signing
KC=~/Library/Keychains/lookout-signing.keychain-db
P=$(cat $S/keychain-pass)
security unlock-keychain -p "$P" $KC

for t in dev dist; do
  kind=$([ $t = dev ] && echo DEVELOPMENT || echo DISTRIBUTION)
  if [ ! -f $S/$t.cer ]; then
    python3 asc.py cert $kind $S/$t.csr $S/$t.cer
  fi
  # .p12 of key + certificate, then import into the signing keychain
  openssl x509 -inform DER -in $S/$t.cer -out $S/$t.pem
  openssl pkcs12 -export -legacy -inkey $S/$t.key -in $S/$t.pem -out $S/$t.p12 -passout pass:"$P" 2>/dev/null \
    || openssl pkcs12 -export -inkey $S/$t.key -in $S/$t.pem -out $S/$t.p12 -passout pass:"$P"
  security import $S/$t.p12 -k $KC -P "$P" -T /usr/bin/codesign -T /usr/bin/security >/dev/null 2>&1 || true
done
chmod 600 $S/*.p12 $S/*.key
# let codesign use the keys without a GUI prompt
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$P" $KC >/dev/null
security find-identity -v -p codesigning $KC
