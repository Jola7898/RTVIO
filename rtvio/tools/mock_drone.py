"""
Stand-in for the drone's MAVLink endpoint, for exercising rtvio.studio's
drone link on one PC without the aircraft.

    python tools/mock_drone.py [--port 14550] [--lat 12.9716 --lon 77.5946]

Accepts the Studio's TCP connection the way the drone's companion computer
does and streams HEARTBEAT / SYS_STATUS / GPS_RAW_INT / ATTITUDE /
GLOBAL_POSITION_INT / VFR_HUD / BATTERY_STATUS for an armed copter flying
a slow circle, plus one STATUSTEXT on connect. It serves no video: in the
Studio's Drone connection card set Drone IP to 127.0.0.1 and Video URL to
any local clip (drone_link paces a file to its own frame rate and loops it),
and the whole drone path - preview, record, Indoor/Outdoor, reconstruct -
runs end to end.

Also used by tests/test_drone_link.py.
"""
import argparse
import math
import socket
import threading
import time

from rtvio.studio import mavlink

EARTH_R = 6378137.0


class MockDrone:
    def __init__(self, host="127.0.0.1", port=14550, lat=12.9716, lon=77.5946, alt_msl=920.0,
                 rel_alt=30.0, radius_m=20.0, speed_mps=4.0, sysid=1, camera=True):
        self.host, self.port = host, port
        self.lat0, self.lon0, self.alt_msl, self.rel_alt = lat, lon, alt_msl, rel_alt
        self.radius_m, self.speed_mps, self.sysid = radius_m, speed_mps, sysid
        self.camera = camera                # answer MAV_CMD_REQUEST_MESSAGE for the camera messages
        self.clients = 0
        self.gcs_heartbeats = 0
        self.stream_requests = 0
        self.camera_requests = 0
        self.interval_commands = []         # msg ids asked for via SET_MESSAGE_INTERVAL
        self._stop = threading.Event()
        self._srv = None

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True, name="mock-drone").start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def pose(self, t):
        """Position/heading on the circle t seconds after connecting."""
        ang = self.speed_mps * t / self.radius_m
        e, n = self.radius_m * math.cos(ang), self.radius_m * math.sin(ang)
        lat = self.lat0 + math.degrees(n / EARTH_R)
        lon = self.lon0 + math.degrees(e / (EARTH_R * math.cos(math.radians(self.lat0))))
        heading = math.degrees(math.atan2(-math.sin(ang), math.cos(ang))) % 360    # direction of travel, from north
        return lat, lon, heading, ang

    def _serve(self, conn):
        self.clients += 1
        parser = mavlink.Parser()
        seq = 0

        def send(msg_name, compid=1, **fields):
            # msg_name, not name: VIDEO_STREAM_INFORMATION has a field called "name".
            nonlocal seq
            conn.sendall(mavlink.encode(msg_name, seq, self.sysid, compid, **fields))
            seq = (seq + 1) & 0xFF

        def camera_reply(msgid):
            # From a camera component (MAV_COMP_ID_CAMERA = 100), like a real camera manager.
            if msgid == mavlink.IDS["CAMERA_INFORMATION"]:
                send("CAMERA_INFORMATION", compid=100, vendor_name="Mock", model_name="Fisheye 1.8mm",
                     focal_length=1.8, sensor_size_h=5.6, sensor_size_v=3.15,
                     resolution_h=1280, resolution_v=720)
            elif msgid == mavlink.IDS["VIDEO_STREAM_INFORMATION"]:
                send("VIDEO_STREAM_INFORMATION", compid=100, name="drone_cam", stream_id=1, count=1,
                     framerate=30.0, resolution_h=1280, resolution_v=720, hfov=140)

        def heartbeat():
            # quadrotor, ArduPilot, armed + custom mode, LOITER, active
            send("HEARTBEAT", type=2, autopilot=mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                 base_mode=1 | 16 | 64 | mavlink.MAV_MODE_FLAG_SAFETY_ARMED, custom_mode=5,
                 system_status=4, mavlink_version=3)

        t0 = time.monotonic()
        next_tick, tick = t0, 0
        conn.settimeout(0.02)
        try:
            heartbeat()
            send("STATUSTEXT", severity=6, text="mock drone ready")
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                    if not data:
                        break
                    for m in parser.feed(data):
                        if m.name == "HEARTBEAT" and m.fields["type"] == mavlink.MAV_TYPE_GCS:
                            self.gcs_heartbeats += 1
                        elif m.name == "REQUEST_DATA_STREAM":
                            self.stream_requests += 1
                        elif (m.name == "COMMAND_LONG"
                              and m.fields["command"] == mavlink.MAV_CMD_SET_MESSAGE_INTERVAL):
                            self.interval_commands.append(int(m.fields["param1"]))
                        elif (m.name == "COMMAND_LONG"
                              and m.fields["command"] == mavlink.MAV_CMD_REQUEST_MESSAGE):
                            self.camera_requests += 1
                            if self.camera:
                                camera_reply(int(m.fields["param1"]))
                except socket.timeout:
                    pass
                now = time.monotonic()
                if now < next_tick:
                    continue
                next_tick += 0.1
                tick += 1
                t = now - t0
                lat, lon, hdg, ang = self.pose(t)
                boot_ms = int(t * 1000)
                v_e, v_n = -self.speed_mps * math.sin(ang), self.speed_mps * math.cos(ang)
                send("GLOBAL_POSITION_INT", time_boot_ms=boot_ms, lat=int(lat * 1e7), lon=int(lon * 1e7),
                     alt=int((self.alt_msl + self.rel_alt) * 1e3), relative_alt=int(self.rel_alt * 1e3),
                     vx=int(v_n * 100), vy=int(v_e * 100), vz=0, hdg=int(hdg * 100))
                send("ATTITUDE", time_boot_ms=boot_ms, roll=0.05, pitch=-0.08, yaw=math.radians(hdg))
                if tick % 3 == 0:
                    send("GPS_RAW_INT", time_usec=int(time.time() * 1e6), fix_type=3,
                         lat=int(lat * 1e7), lon=int(lon * 1e7), alt=int((self.alt_msl + self.rel_alt) * 1e3),
                         eph=70, epv=110, vel=int(self.speed_mps * 100), cog=int(hdg * 100),
                         satellites_visible=14, h_acc=800, v_acc=1200)
                    send("VFR_HUD", airspeed=self.speed_mps, groundspeed=self.speed_mps,
                         alt=self.alt_msl + self.rel_alt, climb=0.0, heading=int(hdg), throttle=45)
                    send("SYS_STATUS", load=250, voltage_battery=15800, current_battery=1250,
                         battery_remaining=76)
                    send("BATTERY_STATUS", voltages=[3950, 3950, 3950, 3950] + [65535] * 6,
                         current_battery=1250, battery_remaining=76, temperature=3100)
                if tick % 10 == 0:
                    heartbeat()
        except OSError:
            pass
        finally:
            conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=14550)
    ap.add_argument("--lat", type=float, default=12.9716)
    ap.add_argument("--lon", type=float, default=77.5946)
    args = ap.parse_args()
    drone = MockDrone(args.host, args.port, args.lat, args.lon).start()
    print("mock drone: MAVLink on %s:%d - in Studio set Drone IP to %s" % (args.host, drone.port, args.host))
    try:
        while True:
            time.sleep(5)
            print("  clients %d, GCS heartbeats %d, stream requests %d"
                  % (drone.clients, drone.gcs_heartbeats, drone.stream_requests), flush=True)
    except KeyboardInterrupt:
        drone.stop()


if __name__ == "__main__":
    main()
