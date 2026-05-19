FROM px4io/px4-dev-simulation-jammy:latest

ARG PX4_VERSION=v1.15.4

RUN git clone --depth 1 --branch "${PX4_VERSION}" https://github.com/PX4/PX4-Autopilot.git /PX4-Autopilot \
    && cd /PX4-Autopilot \
    && git submodule update --init --recursive --depth 1 \
    && git -C platforms/nuttx/NuttX/nuttx fetch --tags

WORKDIR /PX4-Autopilot

COPY px4-daemon-eintr.patch /tmp/px4-daemon-eintr.patch
RUN git apply /tmp/px4-daemon-eintr.patch

RUN make px4_sitl_default

COPY start_px4_sitl.sh /usr/local/bin/start_px4_sitl.sh
RUN chmod +x /usr/local/bin/start_px4_sitl.sh

ENV PX4_HOME_LAT=38.984900 \
    PX4_HOME_LON=-76.478800 \
    PX4_HOME_ALT=45.0 \
    PX4_SYS_AUTOSTART=4001 \
    PX4_SIM_MODEL=gz_x500 \
    PX4_GZ_MODEL=gz_x500 \
    HEADLESS=1

CMD ["/usr/local/bin/start_px4_sitl.sh"]
