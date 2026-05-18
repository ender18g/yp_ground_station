from pymavlink import mavutil

master = mavutil.mavlink_connection(
    'udp:127.0.0.1:14550'
)

master.wait_heartbeat()

print("Connected")

while True:

    msg = master.recv_match(blocking=True)

    if msg:
        print(msg.get_type())