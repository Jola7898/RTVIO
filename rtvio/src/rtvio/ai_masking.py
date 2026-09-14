import os

import numpy as np
import cv2

try:
    from ultralytics import YOLO
    # Suppress YOLO logging if possible
    import logging
    logging.getLogger("ultralytics").setLevel(logging.WARNING)
except ImportError:
    YOLO = None


class DynamicMasker:
    """
    AI-enabled dynamic object masking using YOLO.
    Identifies dynamic classes (people, vehicles, animals) and returns a binary
    mask where dynamic objects are False (to be ignored) and the static background
    is True.

    Two model/class-list presets (session 2+3 finding - see
    HANDOFF_SESSION2.md/3.md): stock COCO-trained yolov8n-seg (this class's
    default, unchanged) detects essentially NOTHING on nadir (straight-down)
    drone footage - confirmed on a real frame with 3 visible parked cars, 0
    detections at any confidence/resolution tested. A car viewed from
    directly overhead looks nothing like COCO's side/oblique training
    images. For nadir footage, use `DynamicMasker.for_nadir_aerial()`
    instead, which loads a YOLOv8 checkpoint fine-tuned on VisDrone (aerial
    imagery, vehicle/pedestrian classes seen from above) - confirmed on the
    same test frame to actually detect the cars (0.86/0.83/0.79 confidence
    at imgsz=1280, vs. 0 for stock COCO at any setting).
    """
    # COCO classes for dynamic objects (ground-level/oblique footage)
    DYNAMIC_CLASSES = {
        0,   # person
        1,   # bicycle
        2,   # car
        3,   # motorcycle
        4,   # airplane (moving in sky)
        5,   # bus
        6,   # train
        7,   # truck
        8,   # boat
        14,  # bird
        15,  # cat
        16,  # dog
        17,  # horse
        18,  # sheep
        19,  # cow
        20,  # elephant
        21,  # bear
        22,  # zebra
        23,  # giraffe
    }

    # VisDrone's own class list is entirely moving/dynamic object types
    # (pedestrian, people, bicycle, car, van, truck, tricycle,
    # awning-tricycle, bus, motor) - unlike COCO there's no "keep this
    # class, it's part of the static scene" subset to carve out, so every
    # class index is dynamic.
    VISDRONE_DYNAMIC_CLASSES = set(range(10))
    _VISDRONE_HF_REPO = "Mahadih534/YoloV8-VisDrone"
    _VISDRONE_HF_FILE = "visDrone.pt"

    def __init__(self, model_size='yolov8n-seg.pt', dynamic_classes=None, imgsz=None):
        self.enabled = YOLO is not None
        self.model = None
        self.dynamic_classes = dynamic_classes if dynamic_classes is not None else self.DYNAMIC_CLASSES
        # Stock COCO models default to ultralytics' own imgsz=640. The
        # VisDrone checkpoint's detections on a 3840x2160 nadir frame went
        # from 2 boxes at 640 to 8 at 1280 (confirmed - see
        # HANDOFF_SESSION3.md) since cars are small in a wide aerial frame;
        # for_nadir_aerial() below sets this to 1280 by default.
        self.imgsz = imgsz
        if self.enabled:
            try:
                # Same convention as vggt_reconstruct._load_vggt: resolve to a
                # stable path under data/models/ instead of letting ultralytics
                # download to whatever the process's CWD happens to be (which
                # otherwise litters the repo root / rtvio/ with yolov8n-seg.pt
                # depending on where the script was launched from).
                if not os.path.isabs(model_size) and os.sep not in model_size and "/" not in model_size:
                    models_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "models")
                    resolved = os.path.abspath(os.path.join(models_dir, model_size))
                    if model_size == self._VISDRONE_HF_FILE and not os.path.exists(resolved):
                        self._download_visdrone(models_dir)
                    model_size = resolved
                # 'n' is the nano model for near real-time performance.
                self.model = YOLO(model_size)
            except Exception as e:
                print(f"Warning: Failed to load YOLO model: {e}")
                self.enabled = False
        else:
            print("Warning: 'ultralytics' not installed. Dynamic object masking disabled.")

    @classmethod
    def for_nadir_aerial(cls):
        """DynamicMasker preset for straight-down drone footage - see the
        class docstring. Downloads the VisDrone checkpoint to data/models/
        on first use (like vggt_reconstruct's VGGT checkpoint, but via
        huggingface_hub since this one's small enough - ~6MB nano-sized -
        that a slow/flaky network isn't the concern the 5GB VGGT checkpoint
        had)."""
        return cls(model_size=cls._VISDRONE_HF_FILE,
                    dynamic_classes=cls.VISDRONE_DYNAMIC_CLASSES, imgsz=1280)

    def _download_visdrone(self, models_dir):
        from huggingface_hub import hf_hub_download
        print("downloading VisDrone-trained YOLOv8 checkpoint (%s) to %s ..."
              % (self._VISDRONE_HF_REPO, models_dir))
        hf_hub_download(self._VISDRONE_HF_REPO, self._VISDRONE_HF_FILE, local_dir=models_dir)

    def get_static_mask(self, image_bgr):
        """
        Takes a BGR image and returns a boolean numpy array of the same
        (height, width) where True means static background and False means
        dynamic object.
        """
        h, w = image_bgr.shape[:2]
        # Default is all static (True)
        mask = np.ones((h, w), dtype=bool)

        if not self.enabled or self.model is None:
            return mask

        # Run inference. verbose=False keeps the console clean during live stream
        kwargs = {"verbose": False, "classes": list(self.dynamic_classes)}
        if self.imgsz is not None:
            kwargs["imgsz"] = self.imgsz
        results = self.model(image_bgr, **kwargs)

        if len(results) > 0:
            result = results[0]
            # If segmentation masks are available
            if result.masks is not None:
                # masks.data is (N, H, W)
                for seg_mask in result.masks.data:
                    # Convert to numpy and resize to original image shape if needed
                    m = seg_mask.cpu().numpy()
                    if m.shape != (h, w):
                        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
                    # Mask out dynamic object pixels
                    mask[m > 0.5] = False
            # Fallback to bounding boxes if masks aren't available for some reason
            elif result.boxes is not None:
                for box in result.boxes.xyxy:
                    x1, y1, x2, y2 = map(int, box.cpu().numpy())
                    x1, y1 = max(0, x1), max(0, y1)
                    x2, y2 = min(w, x2), min(h, y2)
                    mask[y1:y2, x1:x2] = False

        return mask
