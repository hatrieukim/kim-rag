#!/bin/zsh
# Install the kim-rag Zalo sensor (product 10).
#   zalo/install.sh             npm install, then start it with launchd
#   zalo/install.sh --no-start  npm install only
# Then log in once:  cd zalo && npm run login
set -euo pipefail
here=${0:A:h}
repo=${here:h}
label=com.kim.kimrag.zalo

node=$(command -v node) || { echo "node not found; install Node 18 or newer" >&2; exit 1; }
major=$("$node" -p 'process.versions.node.split(".")[0]')
(( major >= 18 )) || { echo "node $major is too old; zca-js needs 18 or newer" >&2; exit 1; }

(cd "$here" && npm ci --no-audit --no-fund)
mkdir -p "$repo/data/logs"
echo "installed dependencies in $here"

[[ ${1:-} == --no-start ]] && exit 0
"$repo/ops/install-agent.sh" "$label"
echo "the sensor runs $node (log: $repo/data/logs/zalo.log)"
echo "if Zalo is not logged in yet: cd $here && npm run login"
