#!/bin/zsh
# Build AppIcon.icns from make-icon.swift. install.sh calls this when the .icns is missing.
#   mac/icon/build.sh   writes mac/icon/AppIcon.icns and mac/icon/AppIcon.png (1024 px preview)
set -euo pipefail
here=${0:A:h}
work=$(mktemp -d)
trap 'rm -rf $work' EXIT
set=$work/AppIcon.iconset
mkdir -p $set
for px in 16 32 64 128 256 512 1024; do
    swift $here/make-icon.swift $work/$px.png $px
done
cp $work/16.png $set/icon_16x16.png
cp $work/32.png $set/icon_16x16@2x.png
cp $work/32.png $set/icon_32x32.png
cp $work/64.png $set/icon_32x32@2x.png
cp $work/128.png $set/icon_128x128.png
cp $work/256.png $set/icon_128x128@2x.png
cp $work/256.png $set/icon_256x256.png
cp $work/512.png $set/icon_256x256@2x.png
cp $work/512.png $set/icon_512x512.png
cp $work/1024.png $set/icon_512x512@2x.png
iconutil -c icns $set -o $here/AppIcon.icns
cp $work/1024.png $here/AppIcon.png
echo "wrote $here/AppIcon.icns"
