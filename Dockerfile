FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/data/wineprefix
ENV LANG=ko_KR.UTF-8
ENV LC_ALL=ko_KR.UTF-8
ENV DISPLAY=:100

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        gnupg2 \
        locales \
        dbus-x11 \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        x11-utils \
        fcitx5 \
        xdotool \
        wmctrl \
        openbox \
        fcitx5-hangul \
        fcitx5-module-dbus \
        fcitx5-frontend-gtk2 \
        fcitx5-frontend-gtk3 \
        fcitx5-frontend-qt5 \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        fonts-noto-core \
        winbind \
        procps \
    && locale-gen ko_KR.UTF-8 \
    && update-locale LANG=ko_KR.UTF-8 \
    && mkdir -pm755 /etc/apt/keyrings \
    && wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
    && wget -O /etc/apt/sources.list.d/winehq-noble.sources https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources \
    && apt-get update \
    && apt-get install -y --install-recommends winehq-staging \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY ["default_drive_c/Program Files/Kakao/KakaoTalk", "/opt/kakao/KakaoTalk"]
COPY docker/entrypoint.sh /usr/local/bin/kakao-entrypoint
COPY docker/run-kakao.sh /usr/local/bin/run-kakao

RUN chmod +x /usr/local/bin/kakao-entrypoint /usr/local/bin/run-kakao \
    && mkdir -p /data

VOLUME ["/data"]
EXPOSE 14500

ENTRYPOINT ["/usr/local/bin/kakao-entrypoint"]
