import serial.tools.list_ports

from numpy     import sin, cos, radians, degrees
from pymavlink import mavutil
from sys       import exit

# -----------------------------------------------------------------------------
def connect_2_cube():
# -----------------------------------------------------------------------------
    
    # Find my connected device information.
    ports = serial.tools.list_ports.comports()

    # Match device ports with devices.
    cube_com_port = None
    for port in ports:
        # Cube.
        if port.vid == 0x2dae:
            cube_com_port = port.device
    
    if cube_com_port == None:
        print('Cube not connected. Exiting.')
        exit()

    cube = mavutil.mavlink_connection(cube_com_port, baud=57600)

    return cube

# -----------------------------------------------------------------------------
def set_message_rate(cube, rate_hz, msg):
# -----------------------------------------------------------------------------

    # Get correct mavlink message id.
    if msg == 'position':
        message_id = mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT
    elif msg == 'imu':
        message_id = mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE
    elif msg == 'servo_out':
        message_id = mavutil.mavlink.MAVLINK_MSG_ID_SERVO_OUTPUT_RAW
    elif msg == 'target position':
        message_id = mavutil.mavlink.MAVLINK_MSG_ID_POSITION_TARGET_GLOBAL_INT
    else:
        msessage_id = None
    
    # Send message to Cube.
    if message_id is not None: 
        # Send message.
        cube.mav.command_long_send(
        cube.target_system,                            # Target system ID
        cube.target_component,                         # Target component ID
        mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,  # ID of command to send
        0,                                             # Confirmation
        message_id,                                    # param1: Message ID to be streamed
        int(1e6 / rate_hz),                            # param2: Interval in microseconds
        0, 0, 0, 0, 0)
        
        # Check for command acknowledgement.
        response = cube.recv_match(type='COMMAND_ACK', blocking=True)

        if (response and response.command == mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
            and response.result == mavutil.mavlink.MAV_RESULT_ACCEPTED):
            res = 'Command succesful.'
        else:
            res = 'Command failed.'
            
        return res
    else:
        res = 'Unknown message.'

# -----------------------------------------------------------------------------
def set_mode(cube, mode):
# -----------------------------------------------------------------------------

    # Get flight modes.
    flight_modes = cube.mode_mapping()

    # Send command.
    cube.mav.command_long_send(
    cube.target_system,                   # Target system ID
    cube.target_component,                # Target component ID
    mavutil.mavlink.MAV_CMD_DO_SET_MODE,  # ID of command to send
    1,                                    # Confirmation
    1,                                    # MAV_MODE_FLAG_CUSTOM_MODE_ENABLED=1
    flight_modes[mode.upper()],
    0, 0, 0, 0, 0)

    # Wait for a response (blocking) to the MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    # command and print result.
    response = cube.recv_match(type='COMMAND_ACK', blocking=True)

    if (response and response.command == mavutil.mavlink.MAV_CMD_DO_SET_MODE
        and response.result == mavutil.mavlink.MAV_RESULT_ACCEPTED):
        res = 'Command successful.'
    else:
        res = 'Command failed.'

    return res
        
# -----------------------------------------------------------------------------        
def arm_system(cube, arm):
# -----------------------------------------------------------------------------
    
    # Send command.
    cube.mav.command_long_send(cube.target_system, cube.target_component,
                                  mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
                                  arm, 21196, 0, 0, 0, 0, 0)

    # Wait for a response (blocking) to the MAV_CMD_COMPONENT_ARM_DISARM
    # command and print result.
    response = cube.recv_match(type='COMMAND_ACK', blocking=True)

    if (response and response.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
            and response.result == mavutil.mavlink.MAV_RESULT_ACCEPTED):
        res = 'Command successful.'
    else:
        res = 'Command failed.'
        
    return res

# -----------------------------------------------------------------------------        
def set_des_lat_lon(cube, lat, lon):
# -----------------------------------------------------------------------------

    # Type mask values.
    # Use Position      : 0b110111111000 / 0x0DF8 / 3576 (decimal)
    # Use Velocity      : 0b110111000111 / 0x0DC7 / 3527 (decimal)
    # Use Acceleration  : 0b110000111000 / 0x0C38 / 3128 (decimal)
    # Use Pos+Vel       : 0b110111000000 / 0x0DC0 / 3520 (decimal)
    # Use Pos+Vel+Accel : 0b110000000000 / 0x0C00 / 3072 (decimal)
    # Use Yaw           : 0b100111111111 / 0x09FF / 2559 (decimal)
    # Use Yaw Rate      : 0b010111111111 / 0x05FF / 1535 (decimal)
    boot_time = 1
    mask      = 3576
    lat_cmd   = int(lat * 1e7)
    lon_cmd   = int(lon * 1e7)
    alt_cmd   = 0
    vx_cmd    = 0
    vy_cmd    = 0
    vz_cmd    = 0
    ax_cmd    = 0
    ay_cmd    = 0
    az_cmd    = 0
    yaw_cmd   = 0
    yaw_rate_cmd = 0
    
    # Send (lat, lon) or directed velocity cmd to vehicle.
    cube.mav.send(mavutil.mavlink.MAVLink_set_position_target_global_int_message(
       boot_time, cube.target_system, cube.target_component, mavutil.mavlink.MAV_FRAME_GLOBAL,
       mask, lat_cmd, lon_cmd, alt_cmd, vx_cmd, vy_cmd, vz_cmd, ax_cmd, ay_cmd,
       az_cmd, yaw_cmd, yaw_rate_cmd))

# -----------------------------------------------------------------------------        
def set_des_spd_hdg(cube, u_ref, hdg_ref):
# -----------------------------------------------------------------------------

    # Type mask values.
    # Use Position      : 0b110111111000 / 0x0DF8 / 3576 (decimal)
    # Use Velocity      : 0b110111000111 / 0x0DC7 / 3527 (decimal)
    # Use Acceleration  : 0b110000111000 / 0x0C38 / 3128 (decimal)
    # Use Pos+Vel       : 0b110111000000 / 0x0DC0 / 3520 (decimal)
    # Use Pos+Vel+Accel : 0b110000000000 / 0x0C00 / 3072 (decimal)
    # Use Yaw           : 0b100111111111 / 0x09FF / 2559 (decimal)
    # Use Yaw Rate      : 0b010111111111 / 0x05FF / 1535 (decimal)

    mask    = 3527
    lat_cmd = int(34.8713500 * 1e7)
    lon_cmd = int(-81.4001010 * 1e7)
    alt_cmd = 0
    vx_cmd  = cos(hdg_ref) * u_ref
    vy_cmd  = sin(hdg_ref) * u_ref
    vz_cmd  = 0
    ax_cmd  = 0
    ay_cmd  = 0
    az_cmd  = 0
    yaw_cmd = 0
    yaw_rate_cmd = 0
    
    # Send (lat, lon) or directed velocity cmd to vehicle.
    cube.mav.send(mavutil.mavlink.MAVLink_set_position_target_global_int_message(
       boot_time, cube.target_system, cube.target_component, mavutil.mavlink.MAV_FRAME_GLOBAL,
       type_mask, lat_cmd, lon_cmd, alt_cmd, vx_cmd, vy_cmd, vz_cmd, ax_cmd, ay_cmd,
       az_cmd, yaw_cmd, yaw_rate_cmd))
    
# -----------------------------------------------------------------------------        
def set_des_hdg(cube, hdg_ref):
# -----------------------------------------------------------------------------
    # Type mask values.
    # Use Position      : 0b110111111000 / 0x0DF8 / 3576 (decimal)
    # Use Velocity      : 0b110111000111 / 0x0DC7 / 3527 (decimal)
    # Use Acceleration  : 0b110000111000 / 0x0C38 / 3128 (decimal)
    # Use Pos+Vel       : 0b110111000000 / 0x0DC0 / 3520 (decimal)
    # Use Pos+Vel+Accel : 0b110000000000 / 0x0C00 / 3072 (decimal)
    # Use Yaw           : 0b100111111111 / 0x09FF / 2559 (decimal)
    # Use Yaw Rate      : 0b010111111111 / 0x05FF / 1535 (decimal)

    type_mask    = 2559
    boot_time = 0
    lat_cmd = 0
    lon_cmd = 0
    alt_cmd = 0
    vx_cmd  = 0
    vy_cmd  = 0
    vz_cmd  = 0
    ax_cmd  = 0
    ay_cmd  = 0
    az_cmd  = 0
    yaw_cmd = hdg_ref
    yaw_rate_cmd = 0
    
    # Send (lat, lon) or directed velocity cmd to vehicle.
    cube.mav.send(mavutil.mavlink.MAVLink_set_position_target_global_int_message(
       boot_time, cube.target_system, cube.target_component, mavutil.mavlink.MAV_FRAME_GLOBAL,
       type_mask, lat_cmd, lon_cmd, alt_cmd, vx_cmd, vy_cmd, vz_cmd, ax_cmd, ay_cmd,
       az_cmd, yaw_cmd, yaw_rate_cmd))
    
# -----------------------------------------------------------------------------   
def get_vehicle_info(cube, veh):
# -----------------------------------------------------------------------------   
    
    # Blocking read.
    BLK_READ = True
    
    # Get current vehicle position.
    pos_msg = cube.recv_match(type='GLOBAL_POSITION_INT', blocking=BLK_READ)

    if pos_msg is not None:

        # Convert the message to dictionary format.
        pos_msg = pos_msg.to_dict()

        # Extract vehicle information.
        veh['lat'] = pos_msg['lat'] * 1e-7
        veh['lon'] = pos_msg['lon'] * 1e-7
        veh['vx']  = pos_msg['vx'] / 100.0
        veh['vy']  = pos_msg['vy'] / 100.0
        veh['vz']  = pos_msg['vz'] / 100.0
        veh['hdg'] = radians(pos_msg['hdg'] / 100.0)
        
        # Calculate body speed.
        veh['u'] = (veh['vx']**2 + veh['vy']**2)**0.5

    # Get imu information.
    imu_msg = cube.recv_match(type='ATTITUDE', blocking=BLK_READ)
        
    if imu_msg is not None:
        imu_msg = imu_msg.to_dict()
        veh['r'] = radians(imu_msg['yawspeed'])

    # Get raw servo output message.
    servo_msg = cube.recv_match(type='SERVO_OUTPUT_RAW', blocking=BLK_READ)

    if servo_msg is not None:
        servo_msg  = servo_msg.to_dict()
        veh['str_pw'] = servo_msg['servo1_raw']
        veh['thr_pw'] = servo_msg['servo3_raw']
        
    return veh

# -----------------------------------------------------------------------------   
def set_rc_override(cube, str_pw, thr_pw):
# -----------------------------------------------------------------------------   

    # Send message to Cube.
    cube.mav.send(mavutil.mavlink.MAVLink_rc_channels_override_message(
        cube.target_system, cube.target_component,
        str_pw, 1500, thr_pw, 1500, 1500, 1500, 1500, 1500))