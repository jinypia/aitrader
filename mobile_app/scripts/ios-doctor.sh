#!/usr/bin/env bash
set -euo pipefail

echo "AITRADER iOS doctor"
echo "-------------------"
echo "Working directory: $(pwd)"
echo

echo "[1] Developer directory"
xcode_select_path="$(xcode-select -p 2>/dev/null || true)"
if [[ -z "${xcode_select_path}" ]]; then
  echo "  - xcode-select path: not configured"
else
  echo "  - xcode-select path: ${xcode_select_path}"
fi

echo
echo "[2] Xcode availability"
if xcodebuild -version >/tmp/aitrader_xcodebuild.txt 2>/tmp/aitrader_xcodebuild_err.txt; then
  cat /tmp/aitrader_xcodebuild.txt | sed 's/^/  - /'
else
  echo "  - Full Xcode is not active yet."
  cat /tmp/aitrader_xcodebuild_err.txt | sed 's/^/    /'
fi

echo
echo "[3] Signing identities"
if security find-identity -v -p codesigning >/tmp/aitrader_signing.txt 2>/tmp/aitrader_signing_err.txt; then
  sed -n '1,20p' /tmp/aitrader_signing.txt | sed 's/^/  - /'
else
  echo "  - Unable to read local signing identities."
  cat /tmp/aitrader_signing_err.txt | sed 's/^/    /'
fi

echo
echo "[4] Capacitor iOS project"
if [[ -d "ios/App" ]]; then
  echo "  - ios/App exists"
else
  echo "  - ios/App is missing"
fi

if [[ -f "ios/App/App.xcworkspace/contents.xcworkspacedata" ]]; then
  echo "  - App.xcworkspace exists"
else
  echo "  - App.xcworkspace not found yet"
fi

echo
echo "[5] Assets"
for asset in \
  "ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png" \
  "ios/App/App/Assets.xcassets/Splash.imageset/splash-2732x2732.png"
do
  if [[ -f "$asset" ]]; then
    echo "  - OK: $asset"
  else
    echo "  - Missing: $asset"
  fi
done

echo
echo "[6] Next recommended command"
echo "  npm run cap:open:ios"
