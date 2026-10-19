#!/bin/zsh
# Install one of the LaunchAgents in ops/ for this checkout, then (re)start it.
#   ops/install-agent.sh com.kim.lifelog.worker
#   ops/install-agent.sh com.kim.lifelog.api
#   ops/install-agent.sh com.kim.kimrag.ios-renew
# mac/install.sh and zalo/install.sh call this for their own agents.
#
# The plists in ops/ are templates. These placeholders are filled in:
#   __REPO__    this repository          __HOME__    your home folder
#   __UV__      the uv binary            __UV_DIR__  the folder uv is in (added to launchd's PATH)
#   __NODE__    the node binary (only needed by the Zalo sensor)
set -euo pipefail
repo=${0:A:h:h}
label=${1:?usage: ops/install-agent.sh <label>}
template=$repo/ops/$label.plist
[[ -f $template ]] || { echo "no such agent: $template" >&2; exit 1; }
plist="$HOME/Library/LaunchAgents/$label.plist"

uv=$(command -v uv || true)
node=$(command -v node || true)
if grep -q '__UV__' $template && [[ -z $uv ]]; then echo "uv not found on PATH" >&2; exit 1; fi
if grep -q '__NODE__' $template && [[ -z $node ]]; then echo "node not found on PATH" >&2; exit 1; fi

mkdir -p "$HOME/Library/LaunchAgents" "$repo/data/logs"
sed -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" -e "s|__UV_DIR__|${uv:h}|g" -e "s|__UV__|$uv|g" \
    -e "s|__NODE__|$node|g" $template > $plist
plutil -lint -s $plist

launchctl bootout "gui/$UID/$label" 2>/dev/null || true
# bootout returns before the old process has exited; bootstrapping too early fails with an I/O error.
for _ in {1..20}; do launchctl print "gui/$UID/$label" &>/dev/null || break; sleep 0.5; done
launchctl bootstrap "gui/$UID" $plist
echo "started $label (logs in $repo/data/logs/)"
