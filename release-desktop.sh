#!/bin/bash
set -e

# Bump the version before running this script.  Edit BOTH files so the
# Tauri binary and the npm package report the same version:
#   desktop/src-tauri/tauri.conf.json   ->   "version": "1.0.1"
#   desktop/package.json                ->   "version": "1.0.1"
# The script reads from tauri.conf.json; the tag is desktop-v<that value>.

VERSION=$(python -c "import json; print(json.load(open('desktop/src-tauri/tauri.conf.json'))['version'])")
TAG="desktop-v$VERSION"
REPO="SaptarshiMazumder/pc-torrent"
ASSET=pc-rent-windows-x64.exe
BUNDLE=desktop/src-tauri/target/release/bundle/nsis

python agent/build_sidecar.py
( cd desktop && npm run tauri build )

cp -f "$BUNDLE"/*-setup.exe "$BUNDLE/$ASSET"

git tag -f "$TAG"
git push origin -f "$TAG"
gh release delete "$TAG" --repo "$REPO" --yes 2>/dev/null || true
gh release create "$TAG" "$BUNDLE/$ASSET" --repo "$REPO" \
  --title "Forge v$VERSION" --generate-notes --latest
