"""
GPU reconstruction queue and GPU-utilisation sampler for rtvio.studio.

Each reconstruction runs as its own `python -m rtvio.vggt_reconstruct`
subprocess, one at a time:
  * a single VGGT job already sizes its windows to fill the card (see
    vggt_reconstruct's auto window size), so two concurrent jobs would only
    fight over VRAM;
  * when the process exits, every byte of VRAM it held is returned - no
    fragmentation or leaked cache carried from one take into the next;
  * a crash, OOM or Cancel kills that job, never the server or the phone link.
Progress comes back through the progress.json file the job rewrites as it
goes (stage, window i/n, ETA), plus its full stdout in job.log.
"""
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque


class GpuMonitor:
    """Samples nvidia-smi once a second. nvidia-smi rather than pynvml: it is
    present wherever the NVIDIA driver is, so this adds no dependency."""

    FIELDS = ("utilization.gpu", "memory.used", "memory.total", "power.draw",
              "temperature.gpu", "name")

    def __init__(self, period_s=1.0, keep=300):
        self.period_s = period_s
        self.samples = deque(maxlen=keep)
        self.name = None
        self.error = None

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="gpu-monitor").start()
        return self

    def _loop(self):
        cmd = ["nvidia-smi", "--query-gpu=" + ",".join(self.FIELDS),
               "--format=csv,noheader,nounits"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        while True:
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                                     creationflags=flags).stdout.strip().splitlines()[0]
                util, mem, total, power, temp, name = [x.strip() for x in out.split(",", 5)]
                self.name = name
                self.samples.append({
                    "t": time.time(), "util": float(util), "mem_mb": float(mem),
                    "mem_total_mb": float(total),
                    "power_w": float(power) if power not in ("[N/A]", "") else None,
                    "temp_c": float(temp),
                })
                self.error = None
            except Exception as e:                       # noqa: BLE001
                self.error = "nvidia-smi unavailable: %s" % e
                time.sleep(10)
            time.sleep(self.period_s)

    def latest(self):
        return self.samples[-1] if self.samples else None

    def since(self, t0):
        return [s for s in self.samples if s["t"] >= t0]

    def snapshot(self, n=120):
        return {"name": self.name, "error": self.error, "samples": list(self.samples)[-n:]}


def next_recon_dir(session_dir):
    k = 1
    while os.path.exists(os.path.join(session_dir, "recon-%d" % k)):
        k += 1
    return os.path.join(session_dir, "recon-%d" % k)


def build_command(session_dir, out_dir, params):
    """Maps the web UI's reconstruction options onto vggt_reconstruct's CLI."""
    cmd = [sys.executable, "-u", "-m", "rtvio.vggt_reconstruct",
           "--from-recording", session_dir, "--out", out_dir,
           "--progress", os.path.join(out_dir, "progress.json")]
    flag_map = {
        "window_frames": "--window-frames", "overlap": "--overlap",
        "frame_stride": "--frame-stride", "conf_percentile": "--conf-percentile",
        "poisson_depth": "--poisson-depth", "gps_mode": "--gps-mode",
        "voxel_factor": "--voxel-factor", "min_views": "--min-views",
        "max_frames": "--max-frames",
    }
    for key, flag in flag_map.items():
        v = params.get(key)
        if v not in (None, "", "auto"):
            cmd += [flag, str(v)]
    if params.get("window_frames") == "auto":
        cmd += ["--window-frames", "auto"]
    if not params.get("masking", False):
        cmd.append("--no-masking")
    if params.get("extras", False):
        cmd.append("--extras")
    return cmd


class ReconJob:
    _ids = iter(range(1, 1 << 30))

    def __init__(self, session_dir, params):
        self.id = next(self._ids)
        self.session_dir = session_dir
        self.session_id = os.path.basename(session_dir.rstrip("\\/"))
        self.params = dict(params)
        self.out_dir = None
        self.state = "queued"
        self.created = time.time()
        self.started = None
        self.finished = None
        self.returncode = None
        self.proc = None
        self.gpu_util_mean = None
        self.gpu_mem_peak_mb = None
        self.error = None

    def progress(self):
        if not self.out_dir:
            return None
        try:
            with open(os.path.join(self.out_dir, "progress.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def log_tail(self, n=40):
        if not self.out_dir:
            return []
        try:
            with open(os.path.join(self.out_dir, "job.log"), encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()[-n:]
        except OSError:
            return []

    def snapshot(self):
        return {
            "id": self.id, "session": self.session_id, "state": self.state,
            "params": self.params,
            "out_dir": os.path.basename(self.out_dir) if self.out_dir else None,
            "created": self.created, "started": self.started, "finished": self.finished,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1) if self.started else None,
            "returncode": self.returncode, "error": self.error,
            "gpu_util_mean": self.gpu_util_mean, "gpu_mem_peak_mb": self.gpu_mem_peak_mb,
            "progress": self.progress(),
        }


class ReconQueue:
    def __init__(self, gpu_monitor=None, cwd=None):
        self.gpu = gpu_monitor
        self.cwd = cwd
        self.jobs = []
        self._lock = threading.Lock()
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._worker, daemon=True, name="recon-worker").start()
        return self

    def submit(self, session_dir, params):
        job = ReconJob(session_dir, params)
        with self._lock:
            self.jobs.append(job)
        self._wake.set()
        print("[recon] queued job %d for %s" % (job.id, job.session_id), flush=True)
        return job

    def cancel(self, job_id):
        with self._lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
        if job is None:
            return False
        if job.state == "queued":
            job.state = "cancelled"
            return True
        if job.state == "running" and job.proc is not None:
            job.state = "cancelling"
            job.proc.terminate()
            return True
        return False

    def busy_with(self, session_id):
        with self._lock:
            return any(j.session_id == session_id and j.state in ("queued", "running", "cancelling")
                       for j in self.jobs)

    def snapshot(self):
        with self._lock:
            return [j.snapshot() for j in self.jobs[-30:]]

    def _next(self):
        with self._lock:
            return next((j for j in self.jobs if j.state == "queued"), None)

    def _worker(self):
        while True:
            job = self._next()
            if job is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            self._run(job)

    def _run(self, job):
        job.out_dir = next_recon_dir(job.session_dir)
        os.makedirs(job.out_dir, exist_ok=True)
        cmd = build_command(job.session_dir, job.out_dir, job.params)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        job.state = "running"
        job.started = time.time()
        print("[recon] job %d: %s" % (job.id, " ".join(cmd)), flush=True)
        with open(os.path.join(job.out_dir, "job.log"), "w", encoding="utf-8") as log:
            log.write("$ %s\n\n" % " ".join(cmd))
            log.flush()
            try:
                job.proc = subprocess.Popen(
                    cmd, stdout=log, stderr=subprocess.STDOUT, cwd=self.cwd, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                job.returncode = job.proc.wait()
            except OSError as e:
                job.error = str(e)
                job.returncode = -1
        job.finished = time.time()
        if self.gpu is not None:
            samples = self.gpu.since(job.started)
            if samples:
                job.gpu_util_mean = round(sum(s["util"] for s in samples) / len(samples), 1)
                job.gpu_mem_peak_mb = max(s["mem_mb"] for s in samples)
        if job.state == "cancelling":
            job.state = "cancelled"
        elif job.returncode == 0:
            job.state = "done"
        else:
            job.state = "failed"
            prog = job.progress() or {}
            job.error = job.error or prog.get("error") or "exit code %s" % job.returncode
        print("[recon] job %d %s (%.0f s, mean GPU util %s%%)"
              % (job.id, job.state, job.finished - job.started, job.gpu_util_mean), flush=True)
