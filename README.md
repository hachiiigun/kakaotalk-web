# KakaoTalk Docker (Windows / WSL)

Windows Docker Desktop 또는 WSL(Windows Subsystem for Linux) 환경의 Docker Engine에서 KakaoTalk PC 버전을 실행하고, 브라우저에서 [voxl](https://github.com/tembo/voxl)로 조작하는 개인용 환경입니다.

## 기능

- KakaoTalk PC 버전 포함
- WineHQ staging 기반 실행
- **브라우저 네이티브 웹 UI (`:14501`)**: 로컬 SQLCipher 4 DB(`TalkUserDB.edb`, `chatListInfo.edb`, `chatLogs_*.edb` + `-wal`)를 메모리에서 복호화해 일반 웹 메신저 UI로 표시하고, SSE 실시간 업데이트·사진/파일 조회·메시지 및 파일 전송을 지원
- **원격 화면 제공 (`:14500`)**: voxl (H.264 + WebCodecs, 최대 60fps, 비밀번호 로그인) — 초기 로그인이나 잠긴 방 수동 열람용으로 병행 제공
- 한글 입력 지원: fcitx5-hangul (오른쪽 Alt·한/영 키·Ctrl+Space로 전환, `docker/fcitx5-config`. voxl 클라이언트는 `docker/voxl-hangul-ime.patch`로 패치해서 빌드)
- 이모지/특수문자 표시용 Noto 폰트 포함
- 화면 깜박임 완화를 위한 소프트웨어 렌더링 설정 포함
- 로그인 상태를 Docker volume에 저장
- Windows 폴더 ↔ 컨테이너 파일 공유 지원

## 설치 및 실행 환경

이 환경은 아래 두 가지 환경을 모두 지원합니다.
1. **Windows Docker Desktop**
2. **WSL2 (Ubuntu 등) 환경에 직접 설치한 Docker Engine (docker-ce)**

---

## 설치 및 설정

### 1. 설정 파일 복사 및 공유 폴더 경로 수정
저장소를 클론한 뒤 `.env.example`을 `.env`로 복사합니다.

```bash
cp .env.example .env
```

`.env` 파일을 열어 공유 폴더 경로(`KAKAO_SHARE_DIR`)를 본인 환경에 맞게 수정합니다.

**Windows Docker Desktop 또는 WSL에서 Windows 폴더를 공유할 경우:**
```env
KAKAO_SHARE_DIR=/mnt/c/Users/사용자이름/Desktop/kakao-share
```

**WSL 전용 경로(Linux 내부 경로)를 공유할 경우:**
```env
KAKAO_SHARE_DIR=/home/사용자이름/kakao-share
```

### 2. 브라우저 옵션 설정 (선택 사항)
웹 브라우저(Firefox)는 기본으로 비활성화되어 있습니다. 카카오톡만 사용할 경우 그대로 두세요.
```env
INSTALL_FIREFOX=false
ENABLE_FIREFOX=false
```

브라우저까지 컨테이너 내부에 포함해서 실행하려면 `.env`에서 아래처럼 바꾼 뒤 빌드합니다.
```env
INSTALL_FIREFOX=true
ENABLE_FIREFOX=true
```

### 3. 공유 폴더 생성
설정한 경로에 맞춰 공유 폴더를 미리 생성해 둡니다.

**Windows Desktop 경로 예시:**
```bash
mkdir -p /mnt/c/Users/사용자이름/Desktop/kakao-share
```

**WSL 내부 경로 예시:**
```bash
mkdir -p ~/kakao-share
```

### 4. 빌드 및 컨테이너 실행
프로젝트 루트 폴더(예: `kakaotalk-web`)에서 아래 명령을 실행합니다.
첫 빌드는 voxl(Rust)을 소스에서 컴파일하므로 수십 분 걸릴 수 있습니다.
*(WSL에 직접 설치한 Docker의 경우 권한에 따라 앞에 `sudo`를 붙여야 할 수 있습니다.)*

```bash
# 빌드
docker compose build --no-cache

# 실행
docker compose up -d
```

### 5. 브라우저로 접속
아래 주소로 접속하면 일반 웹 브라우저에서 카카오톡 대화를 바로 보고 전송할 수 있습니다.
```text
http://localhost:14501/?token=<VOXL_PASSWORD>
```

기존 Wine 원격 화면(voxl)이 필요할 때는 아래 주소로 접속합니다(최초 QR/계정 로그인 등).
```text
http://localhost:14500/?token=<VOXL_PASSWORD>
```

`.env`의 `VOXL_PASSWORD`를 넣습니다. `?token=` 없이 접속하면 브라우저 로그인 창이 뜨며, `14500`(voxl)은 사용자 이름에 아무 값이나 입력하고 비밀번호만 맞으면 됩니다. 비밀번호를 비워 두면 컨테이너가 시작할 때마다 새로 만들어 로그에 출력합니다.

WebCodecs를 지원하는 최신 Chrome/Edge/Firefox가 필요합니다. 주소 뒤에 `&stats`를 붙이면 FPS 오버레이가 표시됩니다.
## 평소 실행

```bash
cd /mnt/c/tmp/kakaotalk-web  # 또는 프로젝트를 클론한 경로
docker compose up -d
```

## Windows 시작 시 자동 실행 등록

Windows 로그인 후 Docker 데몬 준비를 기다렸다가 컨테이너를 띄우고 WSL2 배포판이 유휴 상태로 꺼지지 않게 유지하려면, PowerShell에서 아래 명령어를 실행합니다. 관리자 권한은 필요 없습니다.

```powershell
Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'WSLKakaoTalkWeb' -Value 'wscript.exe "C:\tmp\kakaotalk-web\docker\start-wsl-kakaotalk.vbs"'
```

명령어 동작 원리:

- [start-wsl-kakaotalk.vbs](docker/start-wsl-kakaotalk.vbs): 콘솔 창 없이 숨김(`0`) 모드로 WSL Ubuntu의 부팅 래퍼를 실행합니다.
- [wsl-boot.sh](docker/wsl-boot.sh): `docker info`가 응답할 때까지 최대 60초 대기한 뒤 `docker compose up -d`를 실행합니다.
- 유휴 종료 방지 및 중복 실행 차단: `flock` 잠금으로 단일 `sleep infinity` 유지 세션을 실행해 WSL2 유휴 종료를 막고, 중복 호출은 즉시 종료합니다.
- 부팅 로그: 실행 결과는 `.harness/logs/wsl-boot.log`에 남습니다.

프로젝트를 다른 경로에 클론했다면 `start-wsl-kakaotalk.vbs`, `wsl-boot.sh` 내부 경로(`/mnt/c/tmp/kakaotalk-web`), 레지스트리 경로를 본인 프로젝트 위치에 맞게 수정해야 합니다.

## 파일 전송

Windows 탐색기에서 `.env`에 설정한 공유 폴더에 파일을 넣으면, 카카오톡 파일 첨부창에서 `Z:\share` 폴더로 접근해 바로 선택할 수 있습니다.

voxl 화면의 Files 패널에서도 공유 폴더(`/share`)로 파일을 올리거나 내려받을 수 있습니다. 업로드한 파일은 `Z:\share\Downloads`에 저장됩니다.

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
