#!/usr/bin/env bash
# Shows the toolchain, installs XcodeGen, and makes sure Xcode 26's separately shipped Metal toolchain is
# present (MLX compiles Metal shaders during the build).
set -euo pipefail
# Use the newest Xcode of the chosen major version on the runner. XCODE_MAJOR is a repo variable (default 26).
# Apple, 11-17: from April 2027 uploads need the iOS 27 SDK, so set XCODE_MAJOR=27 before then (a variable, not
# a code change).
major="${XCODE_MAJOR:-26}"
if ! xcodebuild -version | grep -q "^Xcode ${major}"; then
  x=$(ls -d /Applications/Xcode_${major}*.app 2>/dev/null | sort -V | tail -1 || true)
  [ -n "$x" ] || { echo "::error::No Xcode ${major} on this runner."; exit 1; }
  sudo xcode-select -s "$x/Contents/Developer"
fi
sw_vers
xcodebuild -version
xcodebuild -showsdks | grep -i 'iOS ' || true
brew install xcodegen || { brew update && brew install xcodegen; }
xcodebuild -downloadComponent MetalToolchain 2>&1 | tail -n 3 || echo "MetalToolchain download skipped (may already be installed)."
