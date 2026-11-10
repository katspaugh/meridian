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
# streams.py last: a hook that fires during a rebuild must find every module it imports.
cp Sources/workstream_log.py Sources/workstream_links.py Sources/subagents.py Sources/agent_navigation.py Sources/codex_session.py Sources/usage_limits.py \
    Sources/pipeline.py Sources/pipeline_tracker.py \
    Sources/manager.py Sources/manager_store.py Sources/manager_brain.py Sources/manager_role.md "$contents/Resources/"
cp Sources/streams.py "$contents/Resources/"
plugin="$contents/Resources/agent-view"
mkdir -p "$plugin/.claude-plugin" "$plugin/hooks"
cp Assets/agent-view/.claude-plugin/plugin.json "$plugin/.claude-plugin/"
cp Assets/agent-view/hooks/hooks.json Assets/agent-view/hooks/register.js "$plugin/hooks/"
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
