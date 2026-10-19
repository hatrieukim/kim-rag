#!/bin/zsh
# Keep the kim iPhone app signed. A free Apple account signs apps for 7 days only, so the Mac rebuilds the app
# with a fresh profile and reinstalls it over Wi-Fi before the old one expires. Runs every 30 minutes from the
# LaunchAgent com.kim.kimrag.ios-renew; does nothing until the copy on the phone has under RENEW_DAYS left.
#
# Reinstalling keeps the app's data (pending chunks, settings). The app is told first so it closes the chunk
# it is recording; afterwards the Mac launches the new version, which resumes recording on its own.
#
#   ops/ios-renew.sh           renew if due
#   ops/ios-renew.sh --force   rebuild and reinstall now
set -uo pipefail
repo=${0:A:h:h}
ios=$repo/ios
# The team and the iPhone come from ios/Config/Local.xcconfig (see Local.xcconfig.example);
# KIMRAG_IPHONE overrides the iPhone.
local_cfg=$ios/Config/Local.xcconfig
setting() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p" $local_cfg 2>/dev/null | tail -1 }
udid=${KIMRAG_IPHONE:-$(setting IPHONE_UDID)}
bundle=com.kim.kimrag.recorder
team=$(setting DEVELOPMENT_TEAM)
if [[ -z $udid || -z $team ]]; then
    print -u2 "set DEVELOPMENT_TEAM and IPHONE_UDID in $local_cfg (see Local.xcconfig.example)"
    exit 1
fi
app=$ios/build/Build/Products/Release-iphoneos/KimRagRecorder.app
state=$repo/data/ios-installed-expiry        # profile expiry of the copy on the phone, written after each install
warned=$repo/data/ios-renew-warned
renew_s=$(( ${RENEW_DAYS:-2} * 86400 ))
force=${1:-}

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" }
expiry_of() { security cms -D -i "$1/embedded.mobileprovision" 2>/dev/null | plutil -extract ExpirationDate raw -o - - 2>/dev/null }
epoch() { date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null || print 0 }
notify() { osascript -e "display notification \"$1\" with title \"kim iPhone app\"" 2>/dev/null }

now=$(date +%s)
installed=$(cat $state 2>/dev/null || expiry_of $app)
left=$(( $(epoch "$installed") - now ))
if [[ $force != --force ]] && (( left > renew_s )); then exit 0; fi

# 1. A build with a fresh profile. Cached profiles are removed so Xcode asks Apple for new 7-day ones.
built=$(expiry_of $app)
if [[ $force == --force ]] || (( $(epoch "$built") - now < renew_s )); then
    log "rebuilding with a new profile (phone copy expires $installed)"
    for f in ~/Library/Developer/Xcode/UserData/Provisioning\ Profiles/*.mobileprovision(N); do
        id=$(security cms -D -i "$f" 2>/dev/null | plutil -extract Entitlements.application-identifier raw -o - - 2>/dev/null)
        [[ $id == $team.$bundle || $id == $team.$bundle.widget ]] && rm -f "$f"
    done
    if ! xcodebuild -project $ios/KimRagRecorder.xcodeproj -scheme KimRagRecorder -destination 'generic/platform=iOS' \
            -configuration Release -derivedDataPath $ios/build -allowProvisioningUpdates -quiet build; then
        log "build failed"
        notify "Could not rebuild the app. Open Xcode and check the Apple account."
        exit 1
    fi
    built=$(expiry_of $app)
    log "new build expires $built"
fi

# 2. Install when the phone is reachable and unlocked.
locked=$(xcrun devicectl device info lockState --device $udid --json-output - 2>/dev/null \
         | plutil -extract result.passcodeRequired raw -o - - 2>/dev/null)
if [[ $locked == false ]]; then
    # The app closes its current chunk so it can be uploaded before it is replaced.
    xcrun devicectl device notification post --device $udid --name $bundle.prepare-update -q 2>/dev/null && sleep 8
fi
if ! out=$(xcrun devicectl device install app --device $udid $app 2>&1); then
    why=$([[ $out == *locked* ]] && print "phone locked" || print "phone not reachable")
    log "not installed yet ($why); phone copy expires $installed"
    # Under a day left: remind, at most every 3 hours.
    if (( left < 86400 )) && (( now - $(stat -f %m $warned 2>/dev/null || print 0) > 10800 )); then
        notify "Unlock the iPhone near this Mac (same Wi-Fi) so the app can be renewed. It expires $installed."
        touch $warned
    fi
    exit 0
fi
print -r -- $built > $state
rm -f $warned
log "installed; expires $built"
if xcrun devicectl device process launch --device $udid $bundle -q 2>/dev/null; then
    log "app launched, recording resumes"
else
    log "app installed but not launched; it resumes when opened"
    notify "The app was renewed. Open kim on the iPhone to resume recording."
fi
