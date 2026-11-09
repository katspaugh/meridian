#!/bin/sh
set -eu
cd "$(dirname "$0")"
contents=build/Muxteroid.app/Contents
mkdir -p "$contents/MacOS" "$contents/Resources"
swift build -c release
bin=$(swift build -c release --show-bin-path)
cp "$bin/Muxteroid" "$contents/MacOS/"
for bundle in "$bin"/*.bundle; do
    cp -R "$bundle" "$contents/Resources/"
done
cp Sources/streams.py Sources/workstream_links.py "$contents/Resources/"
cp Assets/Muxteroid.icns "$contents/Resources/"
cat > "$contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.muxteroid.app</string>
<key>CFBundleName</key><string>Muxteroid</string>
<key>CFBundleIconFile</key><string>Muxteroid.icns</string>
<key>CFBundleExecutable</key><string>Muxteroid</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1</string>
<key>CFBundleVersion</key><string>1</string>
</dict></plist>
PLIST
codesign --force --sign - build/Muxteroid.app
