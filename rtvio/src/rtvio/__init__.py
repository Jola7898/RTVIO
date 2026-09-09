"""
rtvio — georeferenced 3D reconstruction from UAV video + noisy GPS/IMU,
streamed live from a phone.

See ``docs/STREAMING.md`` for the runtime architecture and
``README.md`` for how to run it. This top-level package intentionally
does not re-export submodules eagerly (importing ``rtvio`` should not
pull in cv2/scipy/laspy just to read ``__version__``); import the
submodule you need, e.g. ``from rtvio.live_pipeline import main``.
"""

__version__ = "0.1.0"
