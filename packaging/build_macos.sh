#!/usr/bin/env bash
# Build the macOS app and a .dmg to hand out.
#
#   ./packaging/build_macos.sh
#
# Unsigned by default, which is fine for your own Mac: first launch needs
# right-click -> Open once, then it opens normally forever after.
#
# To ship it to other teachers you need an Apple Developer ID ($99/yr), so their
# Macs don't block the download. Once you have one, no code changes are needed —
# just set these and re-run:
#
#   export APPLE_DEV_ID="Developer ID Application: Your Name (TEAMID)"
#   export APPLE_ID="you@example.com"
#   export APPLE_TEAM_ID="TEAMID"
#   export APPLE_APP_PASSWORD="abcd-efgh-ijkl-mnop"   # app-specific password
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP_NAME="英语试卷整理工具"
VERSION="$(grep -m1 '^VERSION = ' app/main.py | cut -d'"' -f2)"
PY="${PY:-$ROOT/.venv/bin/python}"

echo "==> Building $APP_NAME v$VERSION"

# Redrawn from source every build, so the shipped icon can never drift from the code
# that defines it (scripts/make_icon.py).
"$PY" scripts/make_icon.py
# Retried: if dist/ is open in Finder it writes a .DS_Store while rm is walking the
# tree, and rm exits "Directory not empty" — which killed the build under `set -e`.
rm -rf build dist || rm -rf build dist

"$PY" -m PyInstaller \
  --name "$APP_NAME" \
  --windowed \
  --noconfirm \
  --clean \
  --osx-bundle-identifier "com.csuderrick.gaokao-english" \
  --icon assets/icon.icns \
  --add-data "assets:assets" \
  --add-data "config:config" \
  --add-data "scripts:scripts" \
  --add-data "prompts:prompts" \
  --paths app \
  --hidden-import settings \
  --paths scripts \
  --hidden-import answer_explanation \
  --hidden-import net_tls \
  --hidden-import deepseek_tokens \
  --hidden-import run_timing \
  --hidden-import docx_blocks \
  --hidden-import docx_splice \
  --hidden-import export_docx_splice \
  --hidden-import export_vocab_docx \
  --hidden-import model_presets \
  --hidden-import providers \
  --hidden-import usage_report \
  --hidden-import pdf_ingest \
  --hidden-import mineru_ingest \
  --hidden-import segment_quality \
  --hidden-import segment_repair \
  --hidden-import bundle_paths \
  --hidden-import gaokao_english_docx_pipeline \
  --collect-all docx \
  --collect-all tokenizers \
  --collect-all truststore \
  --collect-all certifi \
  --collect-all docxcompose \
  --exclude-module streamlit \
  --exclude-module pandas \
  --exclude-module matplotlib \
  app/main.py

APP="dist/$APP_NAME.app"
[ -d "$APP" ] || { echo "!! build produced no .app"; exit 1; }

# Version strings so "check for updates" can compare against a GitHub release.
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion            -string "$VERSION" "$APP/Contents/Info.plist"

# A bundle that opens a window can still die the instant it touches the pipeline,
# so prove the frozen app can reach it before we ship a .dmg.
echo "==> Self-test"
"$APP/Contents/MacOS/$APP_NAME" --selftest

if [ -n "${APPLE_DEV_ID:-}" ]; then
  echo "==> Signing with $APPLE_DEV_ID"
  codesign --force --deep --options runtime --timestamp \
           --sign "$APPLE_DEV_ID" "$APP"

  if [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ]; then
    echo "==> Notarizing (this takes a few minutes)"
    ZIP="dist/notarize.zip"
    ditto -c -k --keepParent "$APP" "$ZIP"
    xcrun notarytool submit "$ZIP" \
      --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
      --password "$APPLE_APP_PASSWORD" --wait
    xcrun stapler staple "$APP"
    rm -f "$ZIP"
  fi
else
  # Ad-hoc signature: no Gatekeeper approval, but the app still runs locally
  # after one right-click -> Open.
  codesign --force --deep --sign - "$APP" 2>/dev/null || true
  echo "==> Unsigned (self-use). First launch: right-click the app -> 开.
    Set APPLE_DEV_ID to produce a signed build for other teachers."
fi

DMG="dist/$APP_NAME-$VERSION.dmg"
echo "==> Packaging $DMG"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO -quiet "$DMG"
rm -rf "$STAGE"

echo
echo "✅ Done"
echo "   App: $APP"
echo "   DMG: $DMG   ($(du -h "$DMG" | cut -f1))"
