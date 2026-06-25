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

mkdir -p /data "$WINEPREFIX/drive_c/Program Files/Kakao"

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

Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension GLX +render -noreset &
xvfb_pid=$!

sleep 1
openbox >/tmp/openbox.log 2>&1 &
/usr/local/bin/run-kakao >/tmp/run-kakao.log 2>&1 &

x11vnc \
  -display "$DISPLAY" \
  -forever \
  -shared \
  -nopw \
  -listen 0.0.0.0 \
  -rfbport 5900 \
  -xkb \
  -noxdamage \
  -repeat \
  -cursor arrow \
  -fixscreen V=3 \
  >/tmp/x11vnc.log 2>&1 &

exec websockify \
  --web=/usr/share/novnc \
  0.0.0.0:14500 \
  localhost:5900
