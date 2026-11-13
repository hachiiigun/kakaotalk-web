FROM ubuntu:24.04 AS voxl-build

ARG VOXL_VERSION=0.3.4
ENV DEBIAN_FRONTEND=noninteractive
ENV PATH=/root/.cargo/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        build-essential \
        pkg-config \
        clang \
        libclang-dev \
        esbuild \
        libx264-dev \
        libyuv-dev \
        libopus-dev \
        libpulse-dev \
        libx11-dev \
        libxext-dev \
        libxcb1-dev \
        patch \
    && curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable

# voxl from its crates.io source, patched so the Korean 한/영 key reaches fcitx5 and
# keys the viewer's own IME swallows are sent by physical key.
COPY docker/voxl-hangul-ime.patch /tmp/voxl-hangul-ime.patch
RUN mkdir -p /src \
    && curl -sSfL "https://crates.io/api/v1/crates/voxl-server/${VOXL_VERSION}/download" | tar -xz -C /src \
    && cd "/src/voxl-server-${VOXL_VERSION}" \
    && patch -p1 < /tmp/voxl-hangul-ime.patch \
    && cargo install --locked --path . --root /opt/voxl

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/data/wineprefix
ENV LANG=ko_KR.UTF-8
ENV LC_ALL=ko_KR.UTF-8
ENV DISPLAY=:100
ARG INSTALL_FIREFOX=true

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --fix-missing --no-install-recommends \
        ca-certificates \
        bzip2 \
        xz-utils \
        wget \
        gnupg2 \
        locales \
        dbus-x11 \
        xvfb \
        libx264-164 \
        libyuv0 \
        libopus0 \
        libpulse0 \
        x11-utils \
        fcitx5 \
        xdotool \
        wmctrl \
        openbox \
        tint2 \
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

COPY --from=voxl-build /opt/voxl/bin/voxl /usr/local/bin/voxl
COPY ["default_drive_c/Program Files/Kakao/KakaoTalk", "/opt/kakao/KakaoTalk"]
COPY docker/entrypoint.sh /usr/local/bin/kakao-entrypoint
COPY docker/run-kakao.sh /usr/local/bin/run-kakao
COPY docker/run-firefox.sh /usr/local/bin/run-firefox
COPY docker/tint2rc /etc/tint2/tint2rc
COPY docker/fcitx5-config /root/.config/fcitx5/config
COPY docker/firefox.desktop /usr/share/applications/firefox.desktop
COPY docker/firefox.xpm /usr/share/pixmaps/firefox.xpm

RUN chmod +x /usr/local/bin/kakao-entrypoint /usr/local/bin/run-kakao /usr/local/bin/run-firefox \
    && mkdir -p /data /opt/firefox \
    && if [ "$INSTALL_FIREFOX" = "true" ]; then \
         wget -q "https://download.mozilla.org/?product=firefox-esr-latest&os=win64&lang=ko" -O /opt/firefox/firefox-setup.exe; \
       else \
         echo "Skipping Windows Firefox installer download"; \
       fi

VOLUME ["/data"]
EXPOSE 14500

ENTRYPOINT ["/usr/local/bin/kakao-entrypoint"]
