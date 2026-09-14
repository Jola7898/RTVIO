"""
RTVIO Studio: one desktop process that drives the whole capture ->
reconstruction loop from a browser.

    python -m rtvio.studio            # then open http://127.0.0.1:8080

  phone_link.py   TCP server the rtvioapk app connects to (protocol v2):
                  sends START/STOP, records every frame of each take to
                  data/sessions/<id>/ in the reconstruct_from_recording layout
  jobs.py         GPU reconstruction queue (one VGGT job at a time, run as a
                  subprocess so its VRAM is fully released between jobs) and
                  an nvidia-smi sampler for the GPU-utilisation readout
  server.py       HTTP API + the web UI in web/
"""
