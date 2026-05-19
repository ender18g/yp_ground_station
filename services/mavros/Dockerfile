FROM ros:noetic-ros-base-focal

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        geographiclib-tools \
        ros-noetic-mavros \
        ros-noetic-mavros-extras \
        ros-noetic-rosbridge-server \
    && geographiclib-get-geoids egm96-5 \
    && rm -rf /var/lib/apt/lists/*
