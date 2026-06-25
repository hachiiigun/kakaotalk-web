# KakaoTalk Wine / Docker Desktop

Windows에서 KakaoTalk PC 버전을 실행하기 위한 개인용 환경입니다. Docker Desktop 방식과 WSL 직접 실행 방식 둘 다 사용할 수 있습니다.

## 기능

- KakaoTalk PC 버전 포함
- WineHQ staging 기반 실행
- 브라우저 접속 화면 제공: noVNC
- 한글 입력 지원: fcitx5-hangul
- 이모지/특수문자 표시용 Noto 폰트 포함
- 화면 깜박임 완화를 위한 소프트웨어 렌더링 설정 포함
- 로그인 상태를 Docker volume에 저장
- Windows 폴더 ↔ 컨테이너 파일 공유 지원
- Docker Desktop 방식: 브라우저에서 noVNC로 조작
- WSL 직접 실행 방식: WSLg 화면에 KakaoTalk 창을 직접 표시

## Docker Desktop으로 설치

저장소를 클론한 뒤 공유 폴더 경로를 설정합니다.

```bash
cp .env.example .env
```

`.env` 파일을 열어 본인 경로로 수정합니다.

```
KAKAO_SHARE_DIR=/mnt/c/Users/사용자이름/Desktop/kakao-share
```

브라우저는 기본으로 포함하지 않습니다. 카카오톡만 사용할 경우 그대로 두세요.

```env
INSTALL_FIREFOX=false
ENABLE_FIREFOX=false
```

브라우저까지 포함하려면 `.env`에서 아래처럼 바꾼 뒤 빌드합니다.

```env
INSTALL_FIREFOX=true
ENABLE_FIREFOX=true
```

공유 폴더를 생성합니다.

```bash
mkdir -p /mnt/c/Users/사용자이름/Desktop/kakao-share
```

빌드합니다.

```bash
docker compose build --no-cache
```

실행합니다.

```bash
docker compose up -d
```

브라우저에서 접속합니다.

```text
http://localhost:14500/vnc.html?autoconnect=true&resize=scale
```

## WSL에서 직접 설치

Docker 없이 WSL 안의 Wine으로 KakaoTalk을 직접 실행할 수도 있습니다. 이 방식은 noVNC 웹 화면이 아니라 Windows 위에 WSLg 창으로 KakaoTalk이 뜹니다.

필수 패키지를 설치합니다.

```bash
apt update && apt install -y winbind winetricks fonts-noto-cjk fonts-noto-color-emoji fcitx5 fcitx5-hangul dbus-x11 x11-apps
```

KakaoTalk 파일을 Wine prefix에 복사합니다.

```bash
export WINEPREFIX="$HOME/.wine-kakao-staging"
mkdir -p "$WINEPREFIX/drive_c/Program Files/Kakao"
cp -a "/mnt/c/tmp/kakaotalk-web/default_drive_c/Program Files/Kakao/KakaoTalk" "$WINEPREFIX/drive_c/Program Files/Kakao/"
```

실행 스크립트를 만듭니다.

```bash
cat > "$HOME/run-kakao.sh" <<'EOF'
#!/usr/bin/env bash
set -e

export WINEPREFIX="$HOME/.wine-kakao-staging"
export LANG=ko_KR.UTF-8
export LC_ALL=ko_KR.UTF-8
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export SDL_IM_MODULE=fcitx
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

pkill -f fcitx5 >/dev/null 2>&1 || true

dbus-run-session -- bash -lc '
export WINEPREFIX="$HOME/.wine-kakao-staging"
export LANG=ko_KR.UTF-8
export LC_ALL=ko_KR.UTF-8
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export SDL_IM_MODULE=fcitx
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

fcitx5 -d --replace >/tmp/fcitx5-kakao.log 2>&1
sleep 2
fcitx5-remote -s hangul || true
fcitx5-remote -o || true

cd "$WINEPREFIX/drive_c/Program Files/Kakao/KakaoTalk"
WINEDEBUG=-all wine KakaoTalk.exe
'
EOF

chmod +x "$HOME/run-kakao.sh"
```

평소에는 아래 명령만 실행합니다.

```bash
~/run-kakao.sh
```

## Docker 평소 실행

```bash
cd /mnt/c/tmp/kakaotalk-web
docker compose up -d
```

## 파일 전송

Windows 탐색기에서 `.env`에 설정한 공유 폴더에 파일을 넣으면, 카카오톡 파일 첨부창에서 `Z:\share` 폴더로 접근해 바로 선택할 수 있습니다.

## Docker 종료

```bash
docker compose down
```

## Docker 완전 초기화

카카오톡 로그인 상태와 Wine 환경까지 지우려면 volume을 같이 삭제합니다.

```bash
docker compose down -v
```

## 주의

- 로컬/개인용으로만 사용하세요.
- 인터넷에 공개 서비스처럼 노출하지 마세요.
- KakaoTalk 바이너리 포함 배포는 KakaoTalk 라이선스와 이용약관을 직접 확인해야 합니다.
