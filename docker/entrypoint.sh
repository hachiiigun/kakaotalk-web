#!/usr/bin/env bash
set -euo pipefail

export WINEPREFIX="${WINEPREFIX:-/data/wineprefix}"
export LANG="${LANG:-ko_KR.UTF-8}"
export LC_ALL="${LC_ALL:-ko_KR.UTF-8}"
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export SDL_IM_MODULE=fcitx
export DISPLAY="${DISPLAY:-:100}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-root}"
export ENABLE_FIREFOX="${ENABLE_FIREFOX:-true}"

mkdir -p /data "$WINEPREFIX/drive_c/Program Files/Kakao"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

if [ ! -d "$WINEPREFIX/drive_c/Program Files/Kakao/KakaoTalk" ]; then
  echo "Initializing KakaoTalk files in $WINEPREFIX"
  cp -a /opt/kakao/KakaoTalk "$WINEPREFIX/drive_c/Program Files/Kakao/"
fi

if [ ! -f "$WINEPREFIX/.initialized" ]; then
  echo "Initializing Wine prefix at $WINEPREFIX"
  wineboot -u || true
  touch "$WINEPREFIX/.initialized"
fi


rm -f /tmp/.X100-lock /tmp/.X11-unix/X100 2>/dev/null || true

Xvfb "$DISPLAY" -screen 0 1280x800x24 -shmem -ac +extension GLX +render -noreset &
xvfb_pid=$!

sleep 1
openbox >/tmp/openbox.log 2>&1 &
sleep 1
if [ "$ENABLE_FIREFOX" = "true" ]; then
  tint2 -c /etc/tint2/tint2rc >/tmp/tint2.log 2>&1 &
else
  sed -e 's/^panel_items = .*/panel_items = TSC/' \
      -e '/^launcher_item_app = /d' \
      /etc/tint2/tint2rc > /tmp/tint2-no-browser.rc
  tint2 -c /tmp/tint2-no-browser.rc >/tmp/tint2.log 2>&1 &
fi
/usr/local/bin/run-kakao >/tmp/run-kakao.log 2>&1 &

# voxl streams $DISPLAY as H.264 over a WebSocket. Login uses VOXL_PASSWORD
# (from .env); without it voxl makes one up and prints the sign-in link to the log.
exec voxl \
  --addr=0.0.0.0 \
  --port=14500 \
  --title=KakaoTalk \
  --audio-enabled=false \
  --files-root=/share
