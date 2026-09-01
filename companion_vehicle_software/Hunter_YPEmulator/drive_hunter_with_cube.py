from can       import Bus, Message
from ctypes    import c_int16
from numpy     import floor, radians, degrees, nan, sin, cos, arctan2, sqrt
from pymavlink import mavutil
from os        import system
from time      import time

# --------------------------------------------------------------
def haversine(lat_frm, lon_frm, lat_to, lon_to):
# --------------------------------------------------------------

    # Convert coordinates to radians
    lat0 = radians(lat_frm)
    lon0 = radians(lon_frm)
    lat  = radians(lat_to)
    lon  = radians(lon_to)

    # Haversine formula.
    delta_lat = lat - lat0
    delta_lon = lon - lon0

    a = pow(sin(delta_lat / 2.0), 2) + cos(lat0) * cos(lat) * pow(sin(delta_lon / 2.0), 2)
    c = 2.0 * arctan2(sqrt(a), sqrt(1.0 - a))

    # Mean radius of the earth (m).
    R = 6378137.0
    d = R * c

    # Bearing to from (lat0,lon0) to (lat,lon)
    theta = arctan2(sin(delta_lon)*cos(lat),
                    cos(lat0)*sin(lat)-sin(lat0)*cos(lat)*cos(delta_lon))

    # Return range and bearing.
    return d, theta

# --------------------------------------------------------------
def set_CAN_mode(veh):
# --------------------------------------------------------------
    # Send message to Hunter to put in CAN mode.
    cmd_id  = 0x421
    dat     = 0x01
    dat_arr = dat.to_bytes(1, byteorder = "big", signed = False)
    tx_msg  = Message(is_extended_id = False, arbitration_id = cmd_id, dlc = 1, data = dat_arr)
    veh.send(tx_msg)

# --------------------------------------------------------------
def set_speed_steer(veh, spd_cmd, str_cmd):
# --------------------------------------------------------------
    # Send control command to Hunter vehicle.
    cmd_id = 0x111

    # Create 8 element list for CAN data.
    dat = [0, 0, 0, 0, 0, 0, 0, 0]

    # Saturate speed/steer commands.
    if spd_cmd >  1.5: spd_cmd =  1.5
    if spd_cmd < -1.5: spd_cmd = -1.5

    if str_cmd > radians(36):  str_cmd = radians(36)
    if str_cmd < radians(-36): str_cmd = radians(-36)

    # Commanded linear speed.
    spd_cmd_int   = int(spd_cmd*1000)
    spd_cmd_bytes = spd_cmd_int.to_bytes(2, "big", signed = True)

    # Commanded steer angle.
    str_cmd_int   = int(-str_cmd*1000)
    str_cmd_bytes = str_cmd_int.to_bytes(2, "big", signed = True)

    # Put in dat array for transmission.
    dat[0] = spd_cmd_bytes[0]
    dat[1] = spd_cmd_bytes[1]
    dat[6] = str_cmd_bytes[0]
    dat[7] = str_cmd_bytes[1]

    # Create CAN message.
    tx_msg = Message(is_extended_id = False, arbitration_id = cmd_id, data = dat)
    veh.send(tx_msg)

# Program constants.
PRT_FRQ =  5
WTC_DOG =  1
CTR_FRQ = 10

# Clear shell/terminal.
system("clear")

# Create connection to Cube.
cube = mavutil.mavlink_connection("tcp:127.0.0.1:14551")

# Initialize program variables.
spd_cmd  = str_cmd  = thr_cmd = 0
lat      = lon      = hdg     = vx = vy = u = nan
pitch    = roll     = r       = nan
x        = y        = nan
mode     = arm      = nan
str_ang  = v_batt   = 0
rng_2_wp = brg_2_wp = nan
lat_d    = lon_d    = nan
hunter_status       = hunter_mode = ""

# Create a connection to Hunter UGV.
with Bus(interface="socketcan", channel="can0") as hunter:

    # Put Hunter UGV in CAN mode.
    set_CAN_mode(hunter)

    # Clear non-critical errors.
    cmd_id  = 0x441
    dat     = 0x00
    dat_arr = dat.to_bytes(1, byteorder = "big", signed = False)
    tx_msg  = Message(is_extended_id = False, arbitration_id = cmd_id, dlc = 1, data = dat_arr)
    hunter.send(tx_msg)

    # Create time epochs.
    t, t0, t_prt, t_dog, t_ctr = 0, time(), 1/PRT_FRQ, 0, 1/CTR_FRQ

    # Primary loop.
    print("Running loop.")
    stop_loop = "no"
    while True:
        
        # Update time.
        t = time() - t0

        # Read CAN message.
        rx_msg = hunter.recv()

        # Extract msg id, source id , priority from arbitrary message.
        msg_id = rx_msg.arbitration_id

        # Get data from message.
        dat = rx_msg.data

        # System Status Feedback Command.    
        if msg_id == 0x211:
            sys_status_veh_body = dat[0]

            if   sys_status_veh_body == 0x00: hunter_status = "Normal"
            elif sys_status_veh_body == 0x01: hunter_status = "Emergency stop"
            elif sys_status_veh_body == 0x02: hunter_status = "System exception"
            else:                             hunter_status = "Unknown"

            mode_control = dat[1]
            if   mode_control == 0x00: hunter_mode = "Standby"
            elif mode_control == 0x01: hunter_mode = "CAN control"
            elif mode_control == 0x03: hunter_mode = "Remote control"
            else:                      hunter_mode = "unknown"
            
            # Hunter battery voltage.
            v_batt = ((dat[2] << 8) + dat[3]) / 10.0

        # Control Frame of movement Control Command
        if msg_id == 0x221:
            
            # Linear speed.
            u = ((dat[0] << 8) + dat[1]) / 1000.0

            # Steer angle.
            str_ang = -c_int16((dat[6] << 8) + dat[7]).value/1000.0
            
        # Get MavLink messages.
        rx_msg = cube.recv_match(blocking=False)

        if rx_msg:
            msg = rx_msg.to_dict()
            
            if msg["mavpackettype"] == "GLOBAL_POSITION_INT":
                lat = msg["lat"]*1e-7
                lon = msg["lon"]*1e-7    
                hdg = radians(msg["hdg"]/100.0)
                vx  = msg["vx"]/100.0
                vy  = msg["vy"]/100.0

            if msg["mavpackettype"] == "ATTITUDE":
                pitch = msg["pitch"] 
                roll  = msg["roll"]      
                r     = msg["yawspeed"]
                
            if msg["mavpackettype"] == "LOCAL_POSITION_NED":
                x = msg["x"]
                y = msg["y"]
            
            if msg["mavpackettype"] == "SERVO_OUTPUT_RAW":
                pass
                #str_cmd = (msg["servo1_raw"] - 1500.0)/500.0*radians(30)
                #thr_cmd = (msg["servo3_raw"] - 1500.0)/500.0

                # print(str_cmd, thr_cmd)
                
            if msg["mavpackettype"] == "HEARTBEAT":

                # Get mode of Cube. MANUAL, GUIDED, etc.
                mode = mavutil.mode_string_v10(rx_msg)
            
                # Check to see arm/disarm status.
                armed = msg["base_mode"] & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                
                if armed: arm = "ARMED"
                else:     arm = "DISARMED"

            if msg["mavpackettype"] == "POSITION_TARGET_GLOBAL_INT":
                
                # Get desired (lat, lon)
                lat_d = msg["lat_int"]/1e7
                lon_d = msg["lon_int"]/1e7
                
                # Pet the watch dog.
                t_dog = time() - t0

            # Reset desired (lat,lon) if watch dog is not happy.
            if (t-t_dog) >= 1/WTC_DOG:
                lat_d = nan
                lon_d = nan

            # Send speed/steer command periodically.
            if t > t_ctr:
            
                if lat_d is not nan:

                    # Calculate range/bearing to waypoint.
                    rng_2_wp, brg_2_wp = haversine(lat, lon, lat_d, lon_d)
                
                    # Make bearing angle compass like.
                    if brg_2_wp < 0: 
                        brg_2_wp += radians(360)

                    # Heading control.
                    e_hdg   = brg_2_wp - hdg

                    # Take shorter route to heading.
                    if e_hdg > radians(180):  e_hdg -= radians(360)
                    if e_hdg < radians(-180): e_hdg += radians(360)

                    str_cmd = 1.0*e_hdg

                    if str_cmd >  radians(36): str_cmd =  radians(36)
                    if str_cmd < -radians(36): str_cmd = -radians(36)

                    # Set speed.
                    if rng_2_wp <= 0.75:
                        spd_cmd = 0.0
                    elif 0.75<rng_2_wp<2.0:
                        spd_cmd = 0.5*rng_2_wp/2.0
                    else:
                        spd_cmd = 0.5
                else:
                    rng_2_wp = brg_2_wp = nan
                    spd_cmd  = str_cmd  = 0.0
            
                if ((mode=="AUTO") or (mode=="GUIDED")) and (arm=="ARMED"):
                    set_speed_steer(hunter, spd_cmd, str_cmd)
                
                # Stop vehicle.
                else:
                    set_speed_steer(hunter, 0, 0)

                # Set next time.
                t_ctr += 1/CTR_FRQ
            
        # Print to terminal
        if t >= t_prt and True:

            # Move to position (row,column). Prevent scrolling.
            r, c = 3, 1
            print(f"\33[{r};{c}H")

            # Calculate time hr:min:sec (EST).
            hrs = floor(t / 3600.0)
            mns = floor((t - hrs * 3600.0) / 60.0)
            scs = round(t) - hrs * 3600.0 - mns * 60.0

            # Print info to screen.
            print(f"Run time : {int(hrs):02}:{int(mns):02}:{int(scs):02}")
            print("----------------------------------------------------------")
            print(f"Hunter system, mode      : {hunter_status}, {hunter_mode}          ")
            print(f"Cube mode, arm           : {mode}, {arm}                           ")
            print()
            print(f"Desired (lat,lon)        : ({lat_d:10.7f},{lon_d:10.7f})")
            print(f"Actual  (lat,lon)        : ({lat:10.7f},{lon:10.7f})")
            print(f"Range to WP          (m) : {rng_2_wp:6.1f}                         ")
            print()
            print(f"Speed_cmd, steer_cmd     : {spd_cmd:6.1f}, {degrees(str_cmd):6.1f} ")
            print(f"Speed (m/s), steer   (d) : {u:6.1f}, {degrees(str_ang):6.1f}       ")
            print()
            print(f"Vehicle heading      (d) : {degrees(hdg):6.1f}                     ")
            print(f"Bearing to WP        (d) : {degrees(brg_2_wp):6.1f}                ")
            
            print(f"Battery              (V) : {v_batt:6.1f}                           ")
            print("----------------------------------------------------------")

            # Update time epoch.
            t_prt += 1/PRT_FRQ

    # Park Hunter.
    set_speed_steer(hunter, 0, 0)

# End of program.
print("\nEnd of line.\n")