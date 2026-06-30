# KakaoTalk Docker (Windows / WSL)

Windows Docker Desktop 또는 WSL(Windows Subsystem for Linux) 환경의 Docker Engine에서 KakaoTalk PC 버전을 실행하고, 브라우저에서 noVNC로 조작하는 개인용 환경입니다.

## 기능

- KakaoTalk PC 버전 포함
- WineHQ staging 기반 실행
- 브라우저 접속 화면 제공: noVNC
- 한글 입력 지원: fcitx5-hangul
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
*(WSL에 직접 설치한 Docker의 경우 권한에 따라 앞에 `sudo`를 붙여야 할 수 있습니다.)*

```bash
# 빌드
docker compose build --no-cache

# 실행
docker compose up -d
```

### 5. 브라우저로 접속
아래 주소로 접속하면 브라우저 안에서 카카오톡 화면이 나타납니다.
```text
http://localhost:14500/vnc.html?autoconnect=true&resize=scale
```
## 평소 실행

```bash
cd /mnt/c/tmp/kakaotalk-web  # 또는 프로젝트를 클론한 경로
docker compose up -d
```

## Windows 시작 시 자동 실행 등록

Windows 로그인 후 자동으로 컨테이너를 실행하려면 PowerShell을 실행한 뒤 아래 명령어를 그대로 입력합니다. 관리자 권한은 필요 없습니다.

```powershell
Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'WSLKakaoTalkWeb' -Value 'powershell.exe -WindowStyle Hidden -Command "wsl --cd /mnt/c/tmp/kakaotalk-web docker compose up -d"'
```

명령어 동작 원리:

- `powershell.exe -WindowStyle Hidden`: 백그라운드에서 동작하도록 실행 창을 숨깁니다.
- `wsl --cd /mnt/c/tmp/kakaotalk-web`: WSL을 켜면서 지정된 디렉터리로 바로 이동합니다.
- `docker compose up -d`: 해당 디렉터리에서 Docker Compose를 백그라운드 모드로 실행합니다.

프로젝트를 다른 경로에 클론했다면 `/mnt/c/tmp/kakaotalk-web` 부분을 본인 프로젝트 경로로 바꿔야 합니다.

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
