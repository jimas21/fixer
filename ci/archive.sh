#!/usr/bin/env bash
# Generates the Xcode project and archives it unsigned. Signing happens once, at export, with Apple's
# cloud-managed distribution certificate. (Signing the archive made a new development certificate on
# every fresh runner, and the account's certificate limit filled up.)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$here/common.sh"
cd "$here/.."

for name in BUNDLE_ID TEAM_ID ASC_KEY_PATH ASC_KEY_ID ASC_ISSUER_ID GITHUB_RUN_NUMBER; do
  [ -n "${!name:-}" ] || die "$name is not set"
done
asc_auth_flags
mkdir -p build

hints() {
  echo "::error::Archive failed. Last error lines from build/archive.log:" >&2
  grep -E 'error:|ARCHIVE FAILED|Cloud signing|No Accounts|No profiles|provisioning' build/archive.log | tail -n 25 >&2 || true
  cat >&2 <<'MSG'

Likely causes, in order:
  1. The API key's Access is not Admin ("Cloud signing permission error"). Make a new Team Key with Admin access.
  2. The bundle ID is not registered, or BUNDLE_ID differs from the registered one.
  3. TEAM_ID is wrong (must match the team that owns the bundle ID and the key).
  4. A pending agreement: the Account Holder must accept it under Business in App Store Connect.
  5. A network blip that looks like an auth error ("No Accounts with App Store Connect Access"): re-run.
MSG
}
trap hints ERR

# The version rolls with every release: major.minor from project.yml, patch = the build number (1.0.55, 1.0.56...).
# Bump major.minor in project.yml by hand for a bigger step. BUILD_OFFSET carries the count over from builds already
# on TestFlight, because a new repo's run numbers restart at 1.
BUILD_NUMBER=$((GITHUB_RUN_NUMBER + ${BUILD_OFFSET:-0}))
BASE_VERSION=$(grep -m1 'MARKETING_VERSION' project.yml | sed 's/.*: *//; s/"//g; s/ *#.*//')
MARKETING_VERSION="$(echo "$BASE_VERSION" | cut -d. -f1-2).$BUILD_NUMBER"
echo "Version: $MARKETING_VERSION  Build number: $BUILD_NUMBER"
xcodegen generate

xcodebuild \
  -project Fixer.xcodeproj \
  -scheme Fixer \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath build/Fixer.xcarchive \
  -derivedDataPath build/DerivedData \
  -skipMacroValidation -skipPackagePluginValidation \
  "${ASC_AUTH[@]}" \
  PRODUCT_BUNDLE_IDENTIFIER="$BUNDLE_ID" \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  CODE_SIGN_STYLE=Automatic \
  CODE_SIGNING_ALLOWED=NO \
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
  MARKETING_VERSION="$MARKETING_VERSION" \
  archive 2>&1 | tee build/archive.log

trap - ERR
test -d build/Fixer.xcarchive || die "xcodebuild reported success but build/Fixer.xcarchive is missing."
# The archive is unsigned, which would drop the entitlements. Ad-hoc sign the app with them so the
# export's distribution signing carries them over (Increased Memory Limit, for the on-device brain).
APP="build/Fixer.xcarchive/Products/Applications/Fixer.app"
if [ -f Config/Fixer.entitlements ] && [ "${FIXER_ENTITLEMENTS:-1}" != "0" ]; then
  if [ -d "$APP/Frameworks" ]; then
    find "$APP/Frameworks" -maxdepth 1 \( -name '*.framework' -o -name '*.dylib' \) -exec codesign --force --sign - {} \;
  fi
  codesign --force --sign - --entitlements Config/Fixer.entitlements --generate-entitlement-der "$APP"
  echo "Entitlements in the archived app:"
  codesign -d --entitlements - "$APP" 2>/dev/null | grep -A1 increased || true
fi
echo "Archive OK."
