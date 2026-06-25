# KakaoTalk Docker Desktop

Windows Docker Desktop에서 KakaoTalk PC 버전을 Docker 컨테이너 안의 Wine으로 실행하고, 브라우저에서 noVNC로 조작하는 개인용 환경입니다.

## 기능

- KakaoTalk PC 버전 포함
- WineHQ staging 기반 실행
- 브라우저 접속 화면 제공: noVNC
- 한글 입력 지원: fcitx5-hangul
- 이모지/특수문자 표시용 Noto 폰트 포함
- 화면 깜박임 완화를 위한 소프트웨어 렌더링 설정 포함
- 로그인 상태를 Docker volume에 저장

## 실행 방법

저장소 폴더로 이동합니다.

```bash
cd /mnt/c/tmp/kakaotalk-web
```

처음 한 번 빌드합니다.

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

종료합니다.

```bash
docker compose down
```

## 평소 실행

한 번 빌드한 뒤에는 보통 아래 명령만 쓰면 됩니다.

```bash
cd /mnt/c/tmp/kakaotalk-web
docker compose up -d
```

## 완전 초기화

카카오톡 로그인 상태와 Wine 환경까지 지우려면 volume을 같이 삭제합니다.

```bash
docker compose down -v
```

## 포함하지 않는 것

- Node.js 웹서버
- Xpra 서버
- Cloudflare Tunnel 설정
- Nginx 설정
- 기존 원본 프로젝트의 공개 웹서비스용 파일
- `node_modules`

## 주의

- 로컬/개인용으로만 사용하세요.
- 인터넷에 공개 서비스처럼 노출하지 마세요.
- KakaoTalk 바이너리 포함 배포는 KakaoTalk 라이선스와 이용약관을 직접 확인해야 합니다.
