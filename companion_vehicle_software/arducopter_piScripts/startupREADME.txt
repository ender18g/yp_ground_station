1. Check that rpicam is connected correctly:

rpicam-hello --list-cameras (should list imx chip)

2. Check if realsense camera is plugged in and responding:

lsusb | grep 8086 (to look for Intel products)

OR

lsusb ( to see all usb devices ) 


3. to start mediamtx piCam streamer:

/usr/local/bin/mediamtx /usr/local/etc/mediamtx.yml

to check video feed: navigate to PI_IP_ADDRESS:8889/cam

4. To start realsense logger

cd realsense_docker_pi
docker compose up

Once running, check topics are live using:
docker compose exec camera bash -c "source /opt/ros/humble/setup.bash && ros2 topic list"
docker compose exec camera bash -c "source /opt/ros/humble/setup.bash && ros2 topic hz /camera/camera/imu"
docker compose exec camera bash -c "source /opt/ros/humble/setup.bash && ros2 topic hz /camera/camera/color/image_raw/compressed"
(ctrl+c to stop logging. look in bag folder for bag file)

5. To start browser GCS interface:

NOTE: may need to modify serve web address based on which machine is running BCS. This can be done by navigating to the pi's hosted webserver and change the GCS IP field to the IP address of the GCS computer

a. Navigate to "arducopter_bcs_bridge": cd arducopter_bcs_bridge
b. Actiavte virtual environment: source /bridgeenv/bin/activate
c. Run bridge script: python arducopter_bridge_wServer.py 


6. Before flight, check that BCS has aircraft registered and that video feed  can be seen
