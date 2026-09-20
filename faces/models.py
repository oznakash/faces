"""Detection + embedding. Tech Spec §4 steps 4-8.

Deliberately delegates alignment to insightface rather than reimplementing the
5-point warp. A subtly wrong alignment does not crash — it quietly degrades every
embedding and looks like "the model is bad". Not worth owning that risk.
"""
import logging
import os
import threading

import cv2
import numpy as np

from .config import MODELS, cfg

log = logging.getLogger("faces.models")
_lock = threading.Lock()
_app = None


def _providers():
    # CoreML is faster on Apple silicon but falls back per-op; CPU is the safe default.
    want = os.environ.get("FACES_PROVIDER", "cpu").lower()
    return ["CoreMLExecutionProvider", "CPUExecutionProvider"] if want == "coreml" \
        else ["CPUExecutionProvider"]


def get_app():
    """Lazily load buffalo_l (SCRFD detector + ArcFace R50). ~300MB on first run."""
    global _app
    with _lock:
        if _app is None:
            from insightface.app import FaceAnalysis
            MODELS.mkdir(parents=True, exist_ok=True)
            app = FaceAnalysis(
                name="buffalo_l",
                root=str(MODELS),
                providers=_providers(),
                allowed_modules=["detection", "recognition"],
            )
            d = int(cfg.detection.det_size)
            app.prepare(ctx_id=-1, det_thresh=float(cfg.detection.det_thresh), det_size=(d, d))
            log.info("models ready (%s, det_size=%d)", _providers()[0], d)
            _app = app
    return _app


def blur_score(bgr: np.ndarray) -> float:
    """Laplacian variance — low means soft/motion-blurred."""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def quality_reject(bbox, det_score, blur) -> str | None:
    """Tech Spec §4 step 5. Returns an exclusion reason, or None to keep."""
    q = cfg.quality
    x1, y1, x2, y2 = bbox
    if min(x2 - x1, y2 - y1) < q.min_bbox_px:
        return "too_small"
    if det_score < q.min_det_score:
        return "low_score"
    if blur is not None and blur < q.min_blur_var:
        return "blurry"
    return None


def crop_face(bgr: np.ndarray, bbox, scale: float = 1.4, out: int = 256) -> np.ndarray:
    """Square crop around the box, padded to stay in frame — the face-wall thumbnail."""
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = max(x2 - x1, y2 - y1) * scale / 2
    x1, y1 = int(max(0, cx - half)), int(max(0, cy - half))
    x2, y2 = int(min(w, cx + half)), int(min(h, cy + half))
    patch = bgr[y1:y2, x1:x2]
    if patch.size == 0:
        return np.zeros((out, out, 3), dtype=np.uint8)
    return cv2.resize(patch, (out, out), interpolation=cv2.INTER_AREA)


def analyze(bgr: np.ndarray):
    """Detect + embed every face. Returns dicts with bbox, score, blur, embedding, crop."""
    faces = get_app().get(bgr)
    out = []
    for f in faces:
        bbox = [int(v) for v in f.bbox]
        crop = crop_face(bgr, bbox)
        b = blur_score(crop)
        emb = getattr(f, "normed_embedding", None)
        out.append({
            "bbox": bbox,
            "det_score": float(f.det_score),
            "blur": b,
            "embedding": None if emb is None else np.asarray(emb, dtype=np.float32),
            "crop": crop,
            "excluded_reason": quality_reject(bbox, float(f.det_score), b),
        })
    return out


def embed_query(bgr: np.ndarray):
    """Selfie path — same code as indexing, by construction (Tech Spec §6 step 3)."""
    faces = get_app().get(bgr)
    faces.sort(key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)
    return [{
        "bbox": [int(v) for v in f.bbox],
        "det_score": float(f.det_score),
        "embedding": np.asarray(f.normed_embedding, dtype=np.float32),
        "crop": crop_face(bgr, [int(v) for v in f.bbox]),
    } for f in faces if getattr(f, "normed_embedding", None) is not None]
