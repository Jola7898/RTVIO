#!/usr/bin/env python3
"""
Reference desktop receiver for the RTVIO Mapper stream.

Run this on the machine the phone should stream to, then point the app's
"Server IP" at this host. It validates the wire format, reports live rates, and
can either dump frames to disk or show them in a live window.

    python mock_receiver.py                       # listen on 0.0.0.0:5555
    python mock_receiver.py --view                # live video window (needs opencv)
    python mock_receiver.py --save-frames out/    # also write JPEGs
    python mock_receiver.py --advertise           # announce over mDNS

One correctness note, because it is the single most common bug when writing a
receiver from the spec: **sock.recv(n) may return fewer than n bytes.** It is a
stream, not a message queue, and a 200 KB JPEG will essentially always arrive
split across several segments. Reading a length field and then calling recv once
works on localhost and fails over WiFi. recv_exact below loops until the
requested count is satisfied.
"""

import argparse
import json
import math
import os
import socket
import struct
import sys
import threading
import time

HEADER_FRAME = 0xFF
HEADER_IMU = 0xFE
HEADER_GPS = 0xFD
HEADER_INTRINSICS = 0xFC
HEADER_HANDSHAKE_ACK = 0xAA

PROTOCOL_VERSION = 1
STATUS_OK = 0

# Sanity ceiling so a desynchronised stream cannot make us allocate wildly.
MAX_JPEG_BYTES = 32 * 1024 * 1024


class StreamClosed(Exception):
    """The peer hung up."""


def recv_exact(sock, count):
    """Read exactly `count` bytes, or raise StreamClosed."""
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise StreamClosed(f"peer closed with {remaining} of {count} bytes outstanding")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_handshake(sock):
    """Desktop -> phone greeting: u8 0xAA, u32 version, u8 status."""
    sock.sendall(struct.pack(">BIB", HEADER_HANDSHAKE_ACK, PROTOCOL_VERSION, STATUS_OK))


class Stats:
    """Counters plus the most recent sample of each kind, for the overlay."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = 0
        self.imu_batches = 0
        self.imu_samples = 0
        self.gps_fixes = 0
        self.bytes = 0
        self.started = time.time()
        self._last_report = self.started
        self._last_frames = 0
        self._last_bytes = 0
        self._last_imu = 0
        self.fps = 0.0
        self.mbps = 0.0
        self.imu_hz = 0.0
        self.first_frame_ms = None
        self.first_imu_ns = None
        self.last_accel = None
        self.last_gyro = None
        self.last_gps = None
        self.frame_size = (0, 0)
        self.jpeg_kb = 0
        self.connected = False
        self.camera_intrinsics = None

    def tick(self, interval=1.0, quiet=False):
        now = time.time()
        elapsed = now - self._last_report
        if elapsed < interval:
            return
        self.fps = (self.frames - self._last_frames) / elapsed
        self.mbps = (self.bytes - self._last_bytes) * 8 / 1e6 / elapsed
        self.imu_hz = (self.imu_samples - self._last_imu) / elapsed
        if not quiet:
            print(
                f"  {self.fps:6.1f} fps | {self.imu_hz:7.1f} IMU Hz | {self.mbps:6.2f} Mbps | "
                f"frames {self.frames:>7,} | imu {self.imu_samples:>8,} | gps {self.gps_fixes}",
                flush=True,
            )
        self._last_report = now
        self._last_frames = self.frames
        self._last_bytes = self.bytes
        self._last_imu = self.imu_samples

    def summary(self):
        dur = max(time.time() - self.started, 1e-9)
        return (
            f"\nSession: {dur:.1f} s\n"
            f"  frames      {self.frames:,}  ({self.frames / dur:.1f} fps avg)\n"
            f"  imu batches {self.imu_batches:,}\n"
            f"  imu samples {self.imu_samples:,}  ({self.imu_samples / dur:.1f} Hz avg)\n"
            f"  gps fixes   {self.gps_fixes:,}\n"
            f"  received    {self.bytes / 1e6:.1f} MB  "
            f"({self.bytes * 8 / 1e6 / dur:.2f} Mbps avg)"
        )


def read_frame(sock, stats, save_dir, sink=None):
    timestamp_ms, width, height, jpeg_size = struct.unpack(">qiii", recv_exact(sock, 20))
    if not 0 < jpeg_size <= MAX_JPEG_BYTES:
        raise StreamClosed(f"implausible jpeg_size {jpeg_size}; stream desynchronised")
    jpeg = recv_exact(sock, jpeg_size)

    with stats.lock:
        stats.frames += 1
        stats.bytes += 21 + jpeg_size
        stats.frame_size = (width, height)
        stats.jpeg_kb = jpeg_size / 1024.0
        first = stats.first_frame_ms is None
        if first:
            stats.first_frame_ms = timestamp_ms

    if first:
        print(f"  first frame: {width}x{height}, {jpeg_size / 1024:.0f} KB, ts={timestamp_ms}")
        if jpeg[:2] != b"\xff\xd8":
            print("  WARNING: payload does not start with the JPEG SOI marker", file=sys.stderr)

    if save_dir:
        path = os.path.join(save_dir, f"frame_{stats.frames:06d}_{timestamp_ms}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg)
    if sink is not None:
        sink.put(jpeg)
    return jpeg


def read_imu(sock, stats):
    (count,) = struct.unpack(">h", recv_exact(sock, 2))
    if count < 0:
        raise StreamClosed(f"negative IMU sample count {count}; stream desynchronised")
    payload = recv_exact(sock, count * 32)

    samples = []
    for i in range(count):
        t_ns, ax, ay, az, gx, gy, gz = struct.unpack_from(">qffffff", payload, i * 32)
        samples.append((t_ns, (ax, ay, az), (gx, gy, gz)))

    with stats.lock:
        stats.imu_batches += 1
        stats.imu_samples += count
        stats.bytes += 3 + count * 32
        first = stats.first_imu_ns is None and samples
        if samples:
            if first:
                stats.first_imu_ns = samples[0][0]
            stats.last_accel = samples[-1][1]
            stats.last_gyro = samples[-1][2]

    if first:
        t_ns, a, g = samples[0]
        print(f"  first IMU:   t={t_ns} ns (since boot)  accel={a}  gyro={g}")
    return samples


def read_gps(sock, stats):
    timestamp_ms, lat, lon, alt, acc = struct.unpack(">qddff", recv_exact(sock, 32))
    with stats.lock:
        stats.gps_fixes += 1
        stats.bytes += 33
        stats.last_gps = (lat, lon, alt, acc)
    print(f"  GPS fix: {lat:.7f}, {lon:.7f}  alt {alt:.1f} m  +/- {acc:.1f} m  ts={timestamp_ms}")
    return timestamp_ms, lat, lon, alt, acc


def read_intrinsics(sock, stats):
    """Read camera intrinsics packet (0xFC header already consumed)."""
    fx, fy, cx, cy = struct.unpack(">ffff", recv_exact(sock, 16))
    k1, k2, p1, p2, k3 = struct.unpack(">ddddd", recv_exact(sock, 40))
    source_len_bytes = recv_exact(sock, 2)
    source_len = struct.unpack(">H", source_len_bytes)[0]
    source = recv_exact(sock, source_len).decode('utf-8') if source_len > 0 else ""

    with stats.lock:
        stats.camera_intrinsics = {
            "fx": fx,
            "fy": fy,
            "cx": cx,
            "cy": cy,
            "k1": k1,
            "k2": k2,
            "p1": p1,
            "p2": p2,
            "k3": k3,
            "source": source
        }
        # Infer width/height from intrinsics if available
        # (fx and width are related: fx = f_mm * width_px / sensor_width_mm)
        # For now we'll store raw values and let the receiver infer if needed
        stats.bytes += 1 + 16 + 40 + 2 + source_len

    print(f"  Camera intrinsics: fx={fx:.1f}, fy={fy:.1f}, cx={cx:.1f}, cy={cy:.1f}")
    print(f"    Distortion: k1={k1:.6f}, k2={k2:.6f}, p1={p1:.6f}, p2={p2:.6f}, k3={k3:.6f}")
    print(f"    Source: {source}")
    return stats.camera_intrinsics


def serve_client(conn, addr, save_dir, stats, sink=None, quiet=False):
    print(f"\nClient connected: {addr[0]}:{addr[1]}")
    send_handshake(conn)
    stats.connected = True
    intrinsics_out_path = None
    try:
        while True:
            header = recv_exact(conn, 1)[0]
            if header == HEADER_FRAME:
                read_frame(conn, stats, save_dir, sink)
            elif header == HEADER_IMU:
                read_imu(conn, stats)
            elif header == HEADER_GPS:
                read_gps(conn, stats)
            elif header == HEADER_INTRINSICS:
                read_intrinsics(conn, stats)
            else:
                raise StreamClosed(f"unknown packet header 0x{header:02X}")
            stats.tick(quiet=quiet)
    except StreamClosed as e:
        print(f"Client {addr[0]} disconnected: {e}")
    except (ConnectionResetError, OSError) as e:
        print(f"Client {addr[0]} dropped: {e}")
    finally:
        stats.connected = False
        print(stats.summary())

        # Export camera intrinsics if received
        if stats.camera_intrinsics and save_dir:
            intrinsics_out_path = os.path.join(save_dir, "camera_intrinsics.json")
            try:
                with open(intrinsics_out_path, "w") as f:
                    json.dump(stats.camera_intrinsics, f, indent=2)
                print(f"  Camera intrinsics saved to {intrinsics_out_path}")
            except Exception as e:
                print(f"  WARNING: could not save intrinsics to {intrinsics_out_path}: {e}",
                      file=sys.stderr)

        # The IMU clock is since-boot and the frame clock is epoch; their
        # difference is the offset the real pipeline needs to associate the two.
        if stats.first_frame_ms is not None and stats.first_imu_ns is not None:
            offset_ms = stats.first_frame_ms - stats.first_imu_ns / 1e6
            print(f"  clock offset (epoch_ms - boot_ms), approx: {offset_ms:.0f} ms")
        conn.close()


# --------------------------------------------------------------------- viewer


class LatestFrame:
    """Single-slot mailbox: the display only ever wants the newest frame."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jpeg = None

    def put(self, jpeg):
        with self._lock:
            self._jpeg = jpeg

    def get(self):
        with self._lock:
            return self._jpeg


# Palette, in OpenCV's BGR order. Values are near-white so they read as data;
# labels are grey so they recede; only health indicators carry colour, which is
# what makes a problem findable at a glance instead of buried in a wall of text.
_FG = (238, 238, 238)
_DIM = (146, 146, 146)
_OK = (120, 220, 130)
_WARN = (70, 190, 255)
_BAD = (70, 70, 240)
_RULE = (70, 70, 70)


def _fps_health(fps, target=25.0):
    if fps >= target:
        return _OK
    return _WARN if fps >= target * 0.6 else _BAD


def draw_overlay(image, stats, target_fps=25.0):
    """
    Draw the telemetry panel onto a decoded frame. Pure; returns the image.

    Laid out as a single left-hand column with a fixed label gutter and
    right-aligned values, so digits line up vertically and a changing number
    does not shift everything around it. Sizes scale with frame width so the
    panel stays legible whether the phone is sending 720p or 1080p.
    """
    import cv2

    with stats.lock:
        fps, mbps, imu_hz = stats.fps, stats.mbps, stats.imu_hz
        frames, imu_samples, gps_fixes = stats.frames, stats.imu_samples, stats.gps_fixes
        accel, gyro, gps = stats.last_accel, stats.last_gyro, stats.last_gps
        w, h = stats.frame_size
        jpeg_kb = stats.jpeg_kb
        connected = stats.connected
        elapsed = time.time() - stats.started

    s = max(0.85, min(2.0, image.shape[1] / 720.0))
    pad = int(14 * s)
    row = int(19 * s)
    panel_w = min(int(330 * s), image.shape[1])
    body, small = cv2.FONT_HERSHEY_DUPLEX, cv2.FONT_HERSHEY_SIMPLEX
    fs, fs_sm = 0.40 * s, 0.36 * s

    def text(x, y, msg, colour=_FG, font=body, scale=fs, weight=1):
        cv2.putText(image, msg, (int(x), int(y)), font, scale, colour, weight, cv2.LINE_AA)

    def right(y, msg, colour=_FG, font=body, scale=fs):
        (tw, _), _ = cv2.getTextSize(msg, font, scale, 1)
        text(panel_w - pad - tw, y, msg, colour, font, scale)

    # ---- measure first, so the slab is exactly as tall as the content ----
    # These fractions must mirror the `y += row * ...` steps below; keeping them
    # in one expression is what stops the slab drifting out of step with the
    # text when a row is added or made conditional.
    rows = 0.4 + 1.5 + 0.95          # header, headline fps, resolution line
    rows += 0.75 + 0.95              # inertial rule + section label
    if accel:
        rows += 1.0
    if gyro:
        rows += 2.0                  # values plus the rotation-rate gauge
    rows += 0.75 + 0.95              # gnss rule + section label
    rows += 2.0 if gps else 1.0
    rows += 0.75 + 0.95              # totals rule + section label
    panel_h = min(int(pad * 2 + row * rows), image.shape[0])

    slab = image[0:panel_h, 0:panel_w]
    dark = slab.copy()
    dark[:] = (16, 16, 16)
    # 0.78 is dark enough to guarantee contrast over a bright sky without
    # hiding the scene the operator is trying to frame.
    image[0:panel_h, 0:panel_w] = cv2.addWeighted(dark, 0.78, slab, 0.22, 0)
    # Accent stripe: signals connection state without spending a whole row.
    cv2.rectangle(image, (0, 0), (int(3 * s), panel_h),
                  _OK if connected else _BAD, -1)

    y = pad + row * 0.4

    # ---- header: status + elapsed --------------------------------------
    dot_r = int(4 * s)
    cv2.circle(image, (pad + dot_r, int(y) - dot_r), dot_r,
               _OK if connected else _BAD, -1, cv2.LINE_AA)
    text(pad + dot_r * 3, y, "LIVE" if connected else "WAITING",
         _OK if connected else _BAD, small, fs_sm)
    mins, secs = divmod(int(elapsed), 60)
    right(y, f"{mins:02d}:{secs:02d}", _DIM, small, fs_sm)

    # ---- headline frame rate -------------------------------------------
    y += row * 1.5
    big = 1.15 * s
    fps_txt = f"{fps:.1f}"
    text(pad, y, fps_txt, _fps_health(fps, target_fps), body, big, 1)
    (tw, _), _ = cv2.getTextSize(fps_txt, body, big, 1)
    text(pad + tw + 6 * s, y, "fps", _DIM, small, fs_sm)
    right(y, f"{mbps:.2f} Mbps", _FG, body, fs)

    y += row * 0.95
    text(pad, y, f"{w} x {h}", _DIM, small, fs_sm)
    right(y, f"{jpeg_kb:.0f} KB/frame", _DIM, small, fs_sm)

    def rule(yy):
        cv2.line(image, (pad, int(yy)), (panel_w - pad, int(yy)), _RULE, 1, cv2.LINE_AA)

    # ---- inertial -------------------------------------------------------
    y += row * 0.75
    rule(y)
    y += row * 0.95
    text(pad, y, "INERTIAL", _DIM, small, fs_sm)
    right(y, f"{imu_hz:.0f} Hz", _OK if imu_hz > 0 else _BAD, body, fs)

    if accel:
        y += row
        text(pad, y, "accel  m/s2", _DIM, small, fs_sm)
        right(y, "{:+.2f}  {:+.2f}  {:+.2f}".format(*accel))
    if gyro:
        y += row
        text(pad, y, "gyro  rad/s", _DIM, small, fs_sm)
        right(y, "{:+.3f}  {:+.3f}  {:+.3f}".format(*gyro))

        # Rotation rate drives motion blur, which is what actually costs the
        # reconstruction feature matches - so it gets a gauge, not just digits.
        y += row
        deg = math.degrees(math.sqrt(sum(v * v for v in gyro)))
        text(pad, y, "rotation", _DIM, small, fs_sm)
        bar_x, bar_w2, bar_h = pad + int(76 * s), int(118 * s), int(6 * s)
        by = int(y) - bar_h
        cv2.rectangle(image, (bar_x, by), (bar_x + bar_w2, by + bar_h), (52, 52, 52), -1)
        frac = min(deg / 120.0, 1.0)
        colour = _OK if deg < 45 else (_WARN if deg < 90 else _BAD)
        if frac > 0:
            cv2.rectangle(image, (bar_x, by),
                          (bar_x + int(bar_w2 * frac), by + bar_h), colour, -1)
        right(y, f"{deg:.0f} deg/s", colour, small, fs_sm)

    # ---- gnss -----------------------------------------------------------
    y += row * 0.75
    rule(y)
    y += row * 0.95
    text(pad, y, "GNSS", _DIM, small, fs_sm)
    if gps:
        lat, lon, alt, acc = gps
        acc_colour = _OK if 0 <= acc <= 5 else (_WARN if 0 <= acc <= 15 else _BAD)
        right(y, f"{gps_fixes} fixes", _DIM, small, fs_sm)
        y += row
        text(pad, y, f"{lat:.6f}, {lon:.6f}")
        y += row
        text(pad, y, "altitude", _DIM, small, fs_sm)
        right(y, f"{alt:.1f} m   +/-{acc:.1f} m", acc_colour)
    else:
        right(y, "no fix", _WARN, small, fs_sm)
        y += row
        text(pad, y, "indoor mode, or no signal yet", _DIM, small, fs_sm)

    # ---- totals ---------------------------------------------------------
    y += row * 0.75
    rule(y)
    y += row * 0.95
    text(pad, y, f"{frames:,} frames", _DIM, small, fs_sm)
    right(y, f"{imu_samples:,} samples", _DIM, small, fs_sm)
    return image


def placeholder(message):
    import cv2
    import numpy as np

    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, message, (20, 180), cv2.FONT_HERSHEY_SIMPLEX,
                0.6, (200, 200, 200), 1, cv2.LINE_AA)
    return img


def run_viewer(stats, sink):
    """Display loop. Must own the main thread - GUI toolkits require it."""
    import cv2
    import numpy as np

    window = "RTVIO Mapper - live stream"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 720, 900)
    print("Viewer open. Press q or Esc in the window to quit.")

    while True:
        jpeg = sink.get()
        if jpeg is None:
            frame = placeholder("Waiting for the phone to connect...")
        else:
            decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            frame = decoded if decoded is not None else placeholder("Frame failed to decode")
            if decoded is not None:
                frame = draw_overlay(frame, stats)

        cv2.imshow(window, frame)
        key = cv2.waitKey(30) & 0xFF
        if key in (ord("q"), 27):
            break
        if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
            break
    cv2.destroyAllWindows()


# ----------------------------------------------------------------------- main


def advertise(port):
    """Announce _rtvio._tcp so the app's discovery button finds this host."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        print("mDNS advertisement needs 'pip install zeroconf'; continuing without it.",
              file=sys.stderr)
        return None

    host_ip = socket.gethostbyname(socket.gethostname())
    info = ServiceInfo(
        "_rtvio._tcp.local.",
        f"RTVIO Receiver ({socket.gethostname()})._rtvio._tcp.local.",
        addresses=[socket.inet_aton(host_ip)],
        port=port,
        properties={"version": str(PROTOCOL_VERSION)},
    )
    zc = Zeroconf()
    zc.register_service(info)
    print(f"Advertising _rtvio._tcp on {host_ip}:{port}")
    return zc, info


def accept_loop(server, save_dir, stats, sink, quiet):
    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        serve_client(conn, addr, save_dir, stats, sink, quiet)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--save-frames", metavar="DIR",
                        help="write every received JPEG into DIR")
    parser.add_argument("--view", action="store_true",
                        help="show the live video in a window (requires opencv-python)")
    parser.add_argument("--advertise", action="store_true",
                        help="announce this receiver over mDNS")
    args = parser.parse_args()

    if args.view:
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            sys.exit("--view needs OpenCV:  pip install opencv-python numpy")

    if args.save_frames:
        os.makedirs(args.save_frames, exist_ok=True)

    zc = advertise(args.port) if args.advertise else None

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(1)
    print(f"RTVIO receiver listening on {args.host}:{args.port}  (Ctrl-C to stop)")

    stats = Stats()
    sink = LatestFrame() if args.view else None

    try:
        if args.view:
            # Networking moves to a worker; the GUI keeps the main thread.
            t = threading.Thread(
                target=accept_loop,
                args=(server, args.save_frames, stats, sink, True),
                daemon=True,
            )
            t.start()
            run_viewer(stats, sink)
        else:
            accept_loop(server, args.save_frames, stats, sink, False)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.close()
        if zc:
            zc[0].unregister_service(zc[1])
            zc[0].close()


if __name__ == "__main__":
    main()
