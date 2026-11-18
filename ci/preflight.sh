#!/usr/bin/env bash
# Fails fast, with a plain message, if any repo variable or secret is missing.
# Checks only that values are non-empty / well-formed. Never prints them.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=common.sh
source "$here/common.sh"

missing=()
for name in BUNDLE_ID TEAM_ID; do
  [ -n "${!name:-}" ] || missing+=("variable $name (Settings > Secrets and variables > Actions > Variables)")
done
for name in ASC_KEY_ID ASC_ISSUER_ID ASC_KEY_P8; do
  [ -n "${!name:-}" ] || missing+=("secret $name (Settings > Secrets and variables > Actions > Secrets)")
done
if [ "${#missing[@]}" -gt 0 ]; then
  printf '::error::Missing %s\n' "${missing[@]}" >&2
  die "Set the items above, then re-run the workflow (Actions > TestFlight > Run workflow)."
fi

case "$BUNDLE_ID" in
  *REPLACE*) die "BUNDLE_ID still contains the REPLACE placeholder. Set the repo variable to the bundle ID you registered at developer.apple.com." ;;
esac
[[ "$BUNDLE_ID" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$BUNDLE_ID" == *.* ]] \
  || die "BUNDLE_ID is not a valid reverse-DNS bundle ID (letters, digits, dots, hyphens; at least one dot)."
[[ "$TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] \
  || die "TEAM_ID must be the 10-character Team ID from developer.apple.com > Membership details."

# Things App Store Connect rejects at upload, checked here so they fail in seconds, not after a 10-minute compile.
problems=()
plutil -lint Config/Info.plist >/dev/null 2>&1 || problems+=("Config/Info.plist is not a valid plist")
grep -q '<key>CFBundleIconName</key>' Config/Info.plist || problems+=("Info.plist has no CFBundleIconName")
grep -q 'ASSETCATALOG_COMPILER_APPICON_NAME' project.yml || problems+=("project.yml has no ASSETCATALOG_COMPILER_APPICON_NAME")
icon_dir="Fixer/Assets.xcassets/AppIcon.appiconset"
if [ -f "$icon_dir/Contents.json" ]; then
  png=$(find "$icon_dir" -name '*.png' | head -1)
  if [ -z "$png" ]; then
    problems+=("no PNG in $icon_dir")
  else
    w=$(sips -g pixelWidth "$png" 2>/dev/null | awk '/pixelWidth/{print $2}')
    a=$(sips -g hasAlpha "$png" 2>/dev/null | awk '/hasAlpha/{print $2}')
    [ "$w" = "1024" ] || problems+=("app icon must be 1024 px wide (is ${w:-unknown})")
    [ "$a" != "yes" ] || problems+=("app icon must have no transparency (alpha)")
  fi
else
  problems+=("no app icon ($icon_dir)")
fi
if [ "${#problems[@]}" -gt 0 ]; then
  printf '::error::%s\n' "${problems[@]}" >&2
  die "App Store Connect would reject this build; fix the items above."
fi

echo "Preflight OK: all five settings present and well-formed (values not printed); app icon and Info.plist OK."
