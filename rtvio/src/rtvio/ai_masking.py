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
    AI-enabled dynamic object masking using YOLOv8-seg.
    Identifies dynamic classes (people, vehicles, animals) and returns a binary
    mask where dynamic objects are False (to be ignored) and the static background
    is True.
    """
    # COCO classes for dynamic objects
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

    def __init__(self, model_size='yolov8n-seg.pt'):
        self.enabled = YOLO is not None
        self.model = None
        if self.enabled:
            try:
                # Same convention as vggt_reconstruct._load_vggt: resolve to a
                # stable path under data/models/ instead of letting ultralytics
                # download to whatever the process's CWD happens to be (which
                # otherwise litters the repo root / rtvio/ with yolov8n-seg.pt
                # depending on where the script was launched from).
                if not os.path.isabs(model_size) and os.sep not in model_size and "/" not in model_size:
                    models_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "models")
                    model_size = os.path.abspath(os.path.join(models_dir, model_size))
                # 'n' is the nano model for near real-time performance.
                self.model = YOLO(model_size)
            except Exception as e:
                print(f"Warning: Failed to load YOLO model: {e}")
                self.enabled = False
        else:
            print("Warning: 'ultralytics' not installed. Dynamic object masking disabled.")

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
        results = self.model(image_bgr, verbose=False, classes=list(self.DYNAMIC_CLASSES))
        
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
