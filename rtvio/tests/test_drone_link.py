"""
Checks for rtvio.studio's drone link (studio/mavlink.py + studio/drone_link.py).

    python tests/test_drone_link.py

CPU-only, no drone and no GPU: telemetry comes from tools/mock_drone.py over
a real localhost socket and video from a small clip written to a temp dir,
so the code under test is the path a real flight takes - TCP connect,
MAVLink parse, video grab, record, finalize - with a different sender.

Imports `rtvio` as an installed package (`pip install -e .` from the repo
root) rather than patching sys.path - see pyproject.toml.
"""
import importlib.util
import json
import os
import shutil
import struct
import tempfile
import threading
import time

import cv2
import numpy as np

from rtvio.studio import mavlink
from rtvio.studio.drone_link import MESSAGE_RATES_HZ, DroneLink

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("mock_drone", os.path.join(HERE, "..", "tools", "mock_drone.py"))
mock_drone = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock_drone)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


def wait_until(cond, timeout):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def load(path):
    with open(path) as f:
        return json.load(f)


# ----------------------------------------------------------------- codec --

def test_crc():
    """The standard CRC-16/MCRF4XX check value. If this is off, every frame
    in both directions is rejected and the link simply looks dead."""
    got = mavlink.x25_crc(b"123456789")
    check("crc: CRC-16/MCRF4XX check value", got == 0x6F91, "0x%04X" % got)


def test_roundtrip():
    frames = [
        mavlink.encode("HEARTBEAT", 7, 1, 1, type=2, autopilot=3, base_mode=209, custom_mode=5,
                       system_status=4, mavlink_version=3),
        mavlink.encode("GLOBAL_POSITION_INT", 8, 1, 1, time_boot_ms=123456, lat=129716000,
                       lon=775946000, alt=950500, relative_alt=30000, vx=-12, vy=400, vz=0, hdg=0),
        mavlink.encode("BATTERY_STATUS", 9, 1, 1, voltages=[4100] * 4 + [65535] * 6,
                       current_battery=1234, battery_remaining=77),
        mavlink.encode("STATUSTEXT", 10, 1, 1, severity=6, text="hello drone"),
    ]
    msgs = mavlink.Parser().feed(b"".join(frames))
    names = [m.name for m in msgs]
    check("roundtrip: every frame decoded",
          names == ["HEARTBEAT", "GLOBAL_POSITION_INT", "BATTERY_STATUS", "STATUSTEXT"], str(names))
    if len(msgs) != 4:
        return
    hb, gp, bat, txt = msgs
    check("roundtrip: HEARTBEAT fields + header",
          hb.fields["custom_mode"] == 5 and hb.fields["base_mode"] == 209 and hb.sysid == 1 and hb.seq == 7)
    check("roundtrip: signed fields",
          gp.fields["lat"] == 129716000 and gp.fields["vx"] == -12 and gp.fields["vy"] == 400)
    # vz and hdg (0) are the payload's last 4 bytes: MAVLink 2 truncates them
    # on the wire, and the parser has to zero-pad them back.
    plen = len(frames[1]) - 12
    check("roundtrip: v2 trailing-zero truncation", plen < 28 and gp.fields["hdg"] == 0,
          "payload %d of 28 bytes" % plen)
    check("roundtrip: array field",
          bat.fields["voltages"][:5] == [4100] * 4 + [65535] and bat.fields["battery_remaining"] == 77)
    check("roundtrip: string field", txt.fields["text"] == "hello drone")


def _v1_frame(name, seq, sysid, compid, **fields):
    """A MAVLink 1 frame, built by hand - some links still carry them."""
    msgid = mavlink.IDS[name]
    _name, crc_extra, fmt, names = mavlink.MESSAGES[msgid]
    payload = struct.pack(fmt, *[fields.get(f, 0) for f in names])
    hdr = bytes((len(payload), seq, sysid, compid, msgid))
    crc = mavlink.x25_crc(bytes((crc_extra,)), mavlink.x25_crc(hdr + payload))
    return bytes((mavlink.STX_V1,)) + hdr + payload + struct.pack("<H", crc)


def test_parser_resync():
    """Garbage before the first frame, a corrupted frame, a message the codec
    does not know and frames split across recv() calls - the ways a TCP read
    hands the parser something other than whole, known, clean frames."""
    bad = bytearray(mavlink.encode("ATTITUDE", 1, 1, 1, roll=0.1, pitch=0.2, yaw=0.3))
    bad[14] ^= 0xFF
    unknown = bytes((mavlink.STX_V2, 3, 0, 0, 2, 1, 1, 0xE7, 0x03, 0x00, 1, 2, 3, 0xAA, 0xBB))  # msg 999
    good = mavlink.encode("GLOBAL_POSITION_INT", 3, 1, 1, lat=1, lon=2, alt=3, hdg=4)
    stream = (b"\x01\x02\x03" + _v1_frame("HEARTBEAT", 0, 1, 1, type=2, autopilot=3, mavlink_version=3)
              + bytes(bad) + unknown + good)
    p = mavlink.Parser()
    out = []
    for i in range(0, len(stream), 7):
        out += p.feed(stream[i:i + 7])
    names = [m.name for m in out]
    check("resync: v1 + v2 frames recovered around bad/unknown ones",
          names == ["HEARTBEAT", "GLOBAL_POSITION_INT"], str(names))
    check("resync: corrupt frame rejected, unknown frame skipped",
          p.crc_errors >= 1 and p.unknown == 1, "crc_errors=%d unknown=%d" % (p.crc_errors, p.unknown))
    check("resync: fields intact after resync", bool(out) and out[-1].fields["hdg"] == 4)


# ------------------------------------------------------------------ link --

def _write_clip(path, n=40, fps=20):
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), fps, (320, 240))
    base = np.random.default_rng(0).integers(0, 255, (240, 320, 3), dtype=np.uint8)
    for i in range(n):
        w.write(np.roll(base, i * 4, axis=1))
    w.release()


def _take(link, finalized, done, seconds):
    finalized.clear()
    done.clear()
    ok, sid = link.start_recording()
    time.sleep(seconds)
    ok2, _ = link.stop_recording()
    got = finalized.wait(15)
    return ok and ok2 and got and done[0][1]["id"] == sid, sid


def test_link_end_to_end():
    tmp = tempfile.mkdtemp(prefix="rtvio_drone_")
    clip = os.path.join(tmp, "clip.avi")
    _write_clip(clip)
    drone = mock_drone.MockDrone(port=0).start()
    done, finalized = [], threading.Event()

    def on_final(d, meta):
        done.append((d, meta))
        finalized.set()

    cfg = {"enabled": True, "ip": "127.0.0.1", "mavlink_port": drone.port, "video_url": clip,
           "mode": "outdoor", "record_long_side": 0, "jpeg_quality": 85, "video_delay_ms": 0}
    link = DroneLink(os.path.join(tmp, "sessions"), cfg, on_session_finalized=on_final)
    link.start()
    try:
        def ready():
            s = link.snapshot()
            return (s["video"]["connected"] and s["telemetry"]["connected"]
                    and s["vehicle_state"].get("gps_fix", 0) >= 3)
        ok = wait_until(ready, 20)
        s = link.snapshot()
        check("link: video + telemetry connected, 3D fix seen", ok,
              "video=%s telemetry=%s" % (s["video"], s["telemetry"]))
        if not ok:
            return
        vs = s["vehicle_state"]
        check("link: vehicle state decoded",
              vs.get("flight_mode") == "LOITER" and vs.get("armed") is True
              and abs(vs.get("lat", 0) - 12.9716) < 0.001 and vs.get("battery_pct") == 76, str(vs))
        wanted = {mavlink.IDS[n] for n in MESSAGE_RATES_HZ}
        check("link: GCS heartbeat + stream requests reach the drone",
              drone.gcs_heartbeats >= 1 and drone.stream_requests >= 1 and wanted <= set(drone.interval_commands),
              "heartbeats=%d requests=%d intervals=%s" % (drone.gcs_heartbeats, drone.stream_requests,
                                                         sorted(set(drone.interval_commands))))
        check("link: preview JPEG available", (link.latest_jpeg or b"")[:2] == b"\xff\xd8")
        got_cam = wait_until(lambda: len(link.snapshot()["camera_reported"]) == 2, 10)
        rep = link.snapshot()["camera_reported"]
        check("link: the drone's own camera description is requested and kept",
              got_cam and rep["VIDEO_STREAM_INFORMATION"]["hfov"] == 140
              and rep["CAMERA_INFORMATION"]["model_name"] == "Fisheye 1.8mm"
              and rep["CAMERA_INFORMATION"]["compid"] == 100,
              "requests=%d reported=%s" % (drone.camera_requests, sorted(rep)))

        ok, sid = _take(link, finalized, done, 2.5)
        check("outdoor: take recorded, finalized and handed over", ok, sid)
        if ok:
            d, meta = done[0]
            times = load(os.path.join(d, "frame_timestamps.json"))
            gps = load(os.path.join(d, "gps_data.json"))
            n_jpg = len([f for f in os.listdir(os.path.join(d, "frames")) if f.endswith(".jpg")])
            check("outdoor: one timestamp per frame on disk",
                  n_jpg == len(times) == meta["frames_received"] and n_jpg >= 30,
                  "%d jpg, %d timestamps" % (n_jpg, len(times)))
            check("outdoor: timestamps start at 0 and increase",
                  times[0] == 0 and all(b > a for a, b in zip(times, times[1:])))
            check("outdoor: a 20 fps source is recorded at ~20 fps", 15 <= meta["fps_mean"] <= 25,
                  "%.1f fps" % meta["fps_mean"])
            check("outdoor: GPS recorded at ~10 Hz", len(gps) >= 15 and meta["gps_fixes"] == len(gps),
                  "%d fixes" % len(gps))
            check("outdoor: GPS on the frames' clock",
                  bool(gps) and -1.0 < gps[0]["timestamp"] < 1.0 and gps[-1]["timestamp"] < times[-1] + 1.0,
                  "gps %.2f..%.2f s, frames 0..%.2f s" % (gps[0]["timestamp"], gps[-1]["timestamp"], times[-1])
                  if gps else "")
            check("outdoor: GPS values are the drone's",
                  bool(gps) and abs(gps[0]["latitude_deg"] - 12.9716) < 0.001 and gps[0]["accuracy_m"] == 0.8)
            check("outdoor: session_meta marks a complete drone outdoor take",
                  meta["origin"] == "drone" and meta["drone_mode"] == "outdoor" and meta["complete"]
                  and meta["vehicle"]["sysid"] == 1)
            tel = load(os.path.join(d, "drone_telemetry.json"))
            check("outdoor: full telemetry log written", len(tel["samples"]) > len(gps),
                  "%d samples" % len(tel["samples"]))

        link.configure(dict(cfg, mode="indoor"))
        check("indoor: a mode change does not reconnect", link.snapshot()["video"]["connected"])
        ok, sid = _take(link, finalized, done, 1.0)
        check("indoor: take recorded", ok, sid)
        if ok:
            d, meta = done[0]
            check("indoor: no GPS written, mode recorded",
                  load(os.path.join(d, "gps_data.json")) == [] and meta["drone_mode"] == "indoor"
                  and meta["gps_fixes"] == 0)

        link.configure(dict(cfg, mode="indoor", mavlink_port=1))
        down = wait_until(lambda: not link.snapshot()["telemetry"]["connected"]
                          and link.snapshot()["telemetry"]["error"], 15)
        check("bad port: telemetry reports why it is down", bool(down),
              str(link.snapshot()["telemetry"]["error"]))
    finally:
        link.configure(dict(cfg, enabled=False))
        drone.stop()
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in (test_crc, test_roundtrip, test_parser_resync, test_link_end_to_end):
        fn()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
