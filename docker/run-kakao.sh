#!/usr/bin/env bash
set -euo pipefail

export WINEPREFIX="${WINEPREFIX:-/data/wineprefix}"
export LANG="${LANG:-ko_KR.UTF-8}"
export LC_ALL="ko_KR.UTF-8"
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export SDL_IM_MODULE=fcitx
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

KAKAOTALK_EXE="${KAKAOTALK_EXE:-$WINEPREFIX/drive_c/Program Files/Kakao/KakaoTalk/KakaoTalk.exe}"

pkill -f fcitx5 >/dev/null 2>&1 || true

exec dbus-run-session -- bash -lc '
set -e
export WINEPREFIX="'"$WINEPREFIX"'"
export LANG=ko_KR.UTF-8
export LC_ALL=ko_KR.UTF-8
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export SDL_IM_MODULE=fcitx
export KAKAOTALK_EXE="'"$KAKAOTALK_EXE"'"
export DISPLAY="${DISPLAY:-:100}"
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

fcitx5 -d --replace >/tmp/fcitx5-kakao.log 2>&1 || true
sleep 5
fcitx5-remote -s hangul >/tmp/fcitx5-remote.log 2>&1 || true
fcitx5-remote -o >>/tmp/fcitx5-remote.log 2>&1 || true

cd "$(dirname "$KAKAOTALK_EXE")"
WINEDEBUG=-all wine "$(basename "$KAKAOTALK_EXE")" &
wine_pid=$!

# Pull the KakaoTalk window into the visible desktop once. Repeating resize/move
# causes Wine rendering flicker in noVNC, so do not keep forcing geometry.
(
  for i in $(seq 1 30); do
    ids=$(xwininfo -root -tree 2>/dev/null | awk '\''/"카카오톡"|\("kakaotalk\.exe" "kakaotalk\.exe"\)/ { print $1 }'\'' || true)
    if [ -n "$ids" ]; then
      for id in $ids; do
        xdotool windowmap "$id" 2>/dev/null || true
        xdotool windowmove "$id" 60 60 2>/dev/null || true
        xdotool windowraise "$id" 2>/dev/null || true
      done
      wmctrl -r "카카오톡" -e 0,60,60,-1,-1 2>/dev/null || true
      wmctrl -a "카카오톡" 2>/dev/null || true
      break
    fi
    sleep 1
  done
) &

wait "$wine_pid"
'
