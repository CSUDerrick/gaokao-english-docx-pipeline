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

APP_NAME="AI英语试卷整理工具"
VERSION="$(grep -m1 '^VERSION = ' app/main.py | cut -d'"' -f2)"
PY="${PY:-$ROOT/.venv/bin/python}"

# Refuse to build the same version twice. The dmg is named after VERSION and that is how
# the teacher archives builds, so two different builds under one number are two files she
# cannot tell apart — and the one already archived is silently not the one she is running.
# Checked before anything else so it costs a second, not a four-minute build.
#
# The record lives here rather than in dist/, which this script wipes on line ~40, and is
# written only after a build actually succeeds so a failed run does not lock you out.
STAMP="$ROOT/packaging/.last_built_version"
if [[ "${ALLOW_SAME_VERSION:-0}" != "1" && -f "$STAMP" && "$(cat "$STAMP")" == "$VERSION" ]]; then
  # Braces are load-bearing: this text is full-width punctuation, and bash reads the
  # bytes of 「，」 as part of the name in "$VERSION，" — "unbound variable", not a message.
  NEXT_BIG="$(echo "$VERSION" | awk -F. '{print $1"."$2+1".0"}')"
  NEXT_SMALL="$(echo "$VERSION" | awk -F. '{print $1"."$2"."$3+1}')"
  cat >&2 <<EOF
❌ 版本号还是 ${VERSION}，和上次打的包同号，已中止。

   老师靠版本号归档，同号的两个 dmg 分不清哪个是哪一版。
   请先改 app/main.py 里的 VERSION（全项目唯一一处）：
     大功能 → 大版本号（${VERSION} → ${NEXT_BIG}）
     小修补 → 小版本号（${VERSION} → ${NEXT_SMALL}）

   确实要重打同一版（刚才构建失败了、或在调打包脚本本身）：
     ALLOW_SAME_VERSION=1 ./packaging/build_macos.sh
EOF
  exit 1
fi

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
  --hidden-import input_precheck \
  --hidden-import answer_pairing \
  --hidden-import notify \
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

# Only now, with a dmg actually on disk: a build that died halfway must not burn the
# version number and force a pointless bump to retry.
printf '%s' "$VERSION" > "$STAMP"

echo
echo "✅ Done"
echo "   App: $APP"
echo "   DMG: $DMG   ($(du -h "$DMG" | cut -f1))"
