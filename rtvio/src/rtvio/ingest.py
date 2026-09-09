"""
Frame-quality and intrinsics helpers shared by the live path.

This used to also define the batch `Dataset` loader (flight.mp4 + sensor
JSON -> in-memory arrays) for the now-removed batch `pipeline.py`. It read
frames back out of the *compressed* flight.mp4 rather than the pristine
frames the Blender step also left behind, deliberately, so the pipeline
faced real video-compression artifacts rather than a documented aspiration.
That loader has no caller left in this repo and was removed rather than
kept as dead weight; `live_pipeline.py` gets frames from the phone stream
instead (see `stream/source.py`).
"""
import cv2
import numpy as np


def sharpness_score(gray_frame):
    """Variance of the Laplacian - a standard, cheap blur detector: a sharp
    image has strong high-frequency edges, so the Laplacian response has
    high variance; a blurred one is smoothed toward a flat response."""
    return cv2.Laplacian(gray_frame, cv2.CV_64F).var()


def downsample_intrinsics(K, orig_wh, target_long_edge):
    """Resize to a target long-edge, scaling fx/fy/cx/cy by the same
    factor. Documented choice: this is a resize (uniform scale of the
    whole frame), not a crop - a crop would also shift cx/cy by the crop
    offset, which a uniform scale does not."""
    w, h = orig_wh
    scale = target_long_edge / max(w, h)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    K2 = K.copy()
    K2[0, 0] *= scale
    K2[1, 1] *= scale
    K2[0, 2] *= scale
    K2[1, 2] *= scale
    return K2, (new_w, new_h), scale
