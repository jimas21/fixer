#!/usr/bin/env bash
# Exports the archive with destination=upload, which signs for distribution and
# sends the build straight to App Store Connect / TestFlight. No local IPA is made.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$here/common.sh"
cd "$here/.."

for name in TEAM_ID ASC_KEY_PATH ASC_KEY_ID ASC_ISSUER_ID; do
  [ -n "${!name:-}" ] || die "$name is not set"
done
asc_auth_flags
test -d build/Fixer.xcarchive || die "build/Fixer.xcarchive not found; the archive step must run first."

cat > build/ExportOptions.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key>
	<string>app-store-connect</string>
	<key>destination</key>
	<string>upload</string>
	<key>teamID</key>
	<string>${TEAM_ID}</string>
	<key>signingStyle</key>
	<string>automatic</string>
	<key>uploadSymbols</key>
	<true/>
	<key>manageAppVersionAndBuildNumber</key>
	<false/>
</dict>
</plist>
PLIST
plutil -lint build/ExportOptions.plist

hints() {
  echo "::error::Export/upload failed. Last error lines from build/export.log:" >&2
  grep -E 'error:|EXPORT FAILED|Cloud signing|No Accounts|No profile|ITMS-|already|agreement' build/export.log | tail -n 25 >&2 || true
  cat >&2 <<'MSG'

Likely causes, in order:
  1. "Cloud signing permission error": the API key is not Admin. Create a new Team Key with Admin access.
  2. "No profile for ... found" even with the key flags: a known, unresolved Apple forum report. Fallback is a
     manually created distribution certificate, or fastlane match with the API key.
  3. No App Store Connect app record for this bundle ID (App Store Connect > Apps > + > New App).
  4. The build number was already uploaded (re-running a job reuses the run number; push a new commit instead),
     or the version is below one already approved ("closed train", codes 90062/90186/90478).
  5. A pending agreement: the Account Holder must accept it under Business in App Store Connect.
MSG
}
trap hints ERR

# Apple's servers fail now and then before anything is uploaded ("Error Downloading App Information", 11-17):
# only that kind of failure is tried again, so a build is never uploaded twice.
trap - ERR
for attempt in 1 2 3; do
  rm -rf build/export
  set +e
  xcodebuild -exportArchive \
    -archivePath build/Fixer.xcarchive \
    -exportOptionsPlist build/ExportOptions.plist \
    -exportPath build/export \
    "${ASC_AUTH[@]}" > build/export.log 2>&1
  status=$?
  set -e
  cat build/export.log
  [ "$status" -eq 0 ] && break
  if [ "$attempt" -lt 3 ] && grep -qE 'Error Downloading App Information|unexpected error occurred|HTTP (500|502|503|504)' build/export.log; then
    echo "Apple's side failed before the upload (attempt $attempt); trying again in $((attempt * 45)) s"
    sleep $((attempt * 45))
    continue
  fi
  hints
  exit "$status"
done

trap - ERR
echo "Export/upload finished. Watch App Store Connect > TestFlight; processing takes a while after the upload."
