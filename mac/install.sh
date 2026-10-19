#!/bin/zsh
# Build and install the kim-rag Mac activity recorder.
#   mac/install.sh            build a release, install the app in ~/Applications, start it with launchd
#   mac/install.sh --no-start build and install the app only, do not start it
set -euo pipefail
here=${0:A:h}
repo=${here:h}
app="$HOME/Applications/KimRag Mac.app"
support="$HOME/Library/Application Support/kim-rag"
label=com.kim.kimrag.mac

swift build -c release --package-path "$here"
bin="$(swift build -c release --package-path "$here" --show-bin-path)/KimRagMac"

mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$support" "$repo/data/logs"
cp "$bin" "$app/Contents/MacOS/KimRagMac"
[[ -f $here/icon/AppIcon.icns ]] || $here/icon/build.sh
cp "$here/icon/AppIcon.icns" "$app/Contents/Resources/AppIcon.icns"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>$label</string>
    <key>CFBundleName</key><string>KimRag Mac</string>
    <key>CFBundleExecutable</key><string>KimRagMac</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.1</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>14.4</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
    <key>NSCalendarsFullAccessUsageDescription</key><string>kim-rag reads meeting titles, times and attendees to name the people in your recordings and to list meetings in the day journal. Notes and links are never read.</string>
    <key>NSMicrophoneUsageDescription</key><string>kim-rag records your side of online meetings from the microphone the meeting app uses and, only when you turn on Room mic in the menu bar, the conversations around the Mac.</string>
    <key>NSAudioCaptureUsageDescription</key><string>kim-rag records the sound of your meeting app (Teams, Zoom, Meet) so the other people in an online meeting are transcribed.</string>
    <key>NSLocationUsageDescription</key><string>kim-rag notes the name of the Wi-Fi network the Mac is on, so the day journal knows where you were. macOS shows that name only to apps allowed to use Location Services.</string>
    <key>NSLocationWhenInUseUsageDescription</key><string>kim-rag notes the name of the Wi-Fi network the Mac is on, so the day journal knows where you were. macOS shows that name only to apps allowed to use Location Services.</string>
    <key>NSCalendarsUsageDescription</key><string>kim-rag reads meeting titles, times and attendees to name the people in your recordings and to list meetings in the day journal. Notes and links are never read.</string>
</dict>
</plist>
PLIST
# Sign with a stable identity so macOS keeps Accessibility and audio permissions across rebuilds.
# An ad-hoc signature changes with every build, which silently voids those permissions.
# Override with KIMRAG_SIGN_IDENTITY; falls back to ad-hoc when no Apple Development cert exists.
identity=${KIMRAG_SIGN_IDENTITY:-$(security find-identity -v -p codesigning | sed -n 's/.*"\(Apple Development: .*\)"/\1/p' | head -1)}
if [[ -z $identity ]]; then
    identity=-
    echo "warning: no Apple Development certificate, signing ad-hoc; permissions reset on every rebuild" >&2
fi
codesign --force --sign "$identity" --identifier "$label" "$app"
echo "signed with: $identity"
cp "$here/kim-rag.zsh" "$support/kim-rag.zsh"
echo "installed $app"

[[ ${1:-} == --no-start ]] && exit 0
"$repo/ops/install-agent.sh" "$label"
