"""Gallery indexing job. Tech Spec §4 — fetch, hash, dedupe, detect, embed, crop, store.

Runs in a background thread per gallery. Fetching is parallel (network bound);
inference is serialized behind one lock (CPU bound, and two ONNX sessions
fighting for the same cores is slower than one).
"""
import hashlib
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import cv2
import httpx
import numpy as np

from . import adapters, cluster, db, models
from .config import CROPS, cfg

log = logging.getLogger("faces.pipeline")

_infer_lock = threading.Lock()
_jobs: dict[str, threading.Thread] = {}
_subs: dict[str, list[queue.Queue]] = {}
_subs_lock = threading.Lock()


# ---------------------------------------------------------------- events (SSE)
def subscribe(gallery_id: str) -> queue.Queue:
    q = queue.Queue()
    with _subs_lock:
        _subs.setdefault(gallery_id, []).append(q)
    return q


def unsubscribe(gallery_id: str, q: queue.Queue):
    with _subs_lock:
        if gallery_id in _subs and q in _subs[gallery_id]:
            _subs[gallery_id].remove(q)


def publish(gallery_id: str, event: str, data: dict):
    with _subs_lock:
        for q in _subs.get(gallery_id, []):
            q.put((event, data))


def _status(con, gid):
    r = con.execute("SELECT * FROM galleries WHERE id=?", (gid,)).fetchone()
    return dict(r) if r else None


def _set(con, gid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    with con:
        con.execute(f"UPDATE galleries SET {cols} WHERE id=?", (*kw.values(), gid))


# ---------------------------------------------------------------- public API
def start(url: str) -> tuple[str, bool]:
    """Create-or-reuse a gallery for this URL and kick off indexing. Returns (id, cached)."""
    canon = adapters.canonical(url)
    con = db.connect()
    row = con.execute("SELECT * FROM galleries WHERE url_canonical=?", (canon,)).fetchone()
    if row and row["status"] in ("ready", "partial"):
        con.close()
        return row["id"], True
    if row and row["status"] in ("queued", "ingesting", "processing") and row["id"] in _jobs \
            and _jobs[row["id"]].is_alive():
        con.close()
        return row["id"], False
    if row:                                            # failed or orphaned: start over
        with con:
            con.execute("DELETE FROM galleries WHERE id=?", (row["id"],))
    gid = db.new_id()
    with con:
        con.execute(
            "INSERT INTO galleries (id,url_canonical,host,adapter,status,created_at,expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (gid, canon, urlparse(canon).netloc, None, "queued", db.now(), db.ttl()))
    con.close()
    t = threading.Thread(target=_run, args=(gid, canon), daemon=True, name=f"index-{gid[:8]}")
    _jobs[gid] = t
    t.start()
    return gid, False


def delete(gallery_id: str):
    con = db.connect()
    keys = [r["crop_key"] for r in con.execute(
        "SELECT crop_key FROM faces WHERE gallery_id=? AND crop_key IS NOT NULL", (gallery_id,))]
    with con:
        con.execute("DELETE FROM galleries WHERE id=?", (gallery_id,))
    con.close()
    for k in keys:
        try:
            (CROPS / k).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- the job
def _fetch_best(client: httpx.Client, row, og_cache: dict) -> tuple[bytes, str]:
    """Upgraded URL first; og:image of the photo page when no rewrite rule applied;
    the harvested thumbnail as the last resort. Returns (bytes, url_used)."""
    urls = [row["source_url"]]
    no_rule_applied = row["source_url"] == row["fallback_url"]
    if no_rule_applied and row["page_url"] and row["page_url"] != row["source_url"]:
        if row["page_url"] not in og_cache:
            og_cache[row["page_url"]] = adapters.og_image(client, row["page_url"])
        og = og_cache[row["page_url"]]
        if og and og not in urls:
            urls.insert(0, og)
    if row["fallback_url"] and row["fallback_url"] not in urls:
        urls.append(row["fallback_url"])
    last = None
    for u in urls:
        try:
            return _fetch(client, u), u
        except Exception as e:                       # noqa: BLE001
            last = e
    raise last or ValueError("FETCH_FAILED")


def _fetch(client: httpx.Client, url: str) -> bytes:
    with client.stream("GET", url, timeout=25) as r:
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if not ct.startswith("image/"):
            raise ValueError("NOT_AN_IMAGE")
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > cfg.limits.max_image_bytes:
                raise ValueError("TOO_LARGE")
        return bytes(buf)


def _process_one(con, gid, img_row, data: bytes) -> tuple[int, int]:
    """Decode -> detect -> embed -> crop -> persist. Returns (kept, excluded)."""
    sha = hashlib.sha256(data).hexdigest()
    dup = con.execute(
        "SELECT id FROM images WHERE gallery_id=? AND content_sha256=? AND id!=?",
        (gid, sha, img_row["id"])).fetchone()
    if dup:
        with con:
            con.execute("UPDATE images SET status='duplicate', content_sha256=? WHERE id=?",
                        (sha, img_row["id"]))
        return 0, 0

    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise ValueError("DECODE_FAILED")
    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest > 1600:
        s = 1600 / longest
        arr = cv2.resize(arr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)

    with _infer_lock:
        found = models.analyze(arr)

    kept = excluded = 0
    rows = []
    for f in found:
        fid = db.new_id()
        crop_key = None
        if f["excluded_reason"] is None and f["embedding"] is not None:
            crop_key = f"{gid[:8]}/{fid}.webp"
            (CROPS / gid[:8]).mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(CROPS / crop_key), f["crop"], [cv2.IMWRITE_WEBP_QUALITY, 82])
            kept += 1
        else:
            excluded += 1
        rows.append((
            fid, gid, img_row["id"], ",".join(map(str, f["bbox"])), f["det_score"], f["blur"],
            db.pack(f["embedding"]) if (f["excluded_reason"] is None and f["embedding"] is not None) else None,
            crop_key, f["excluded_reason"]))
    with con:
        con.executemany(
            "INSERT INTO faces (id,gallery_id,image_id,bbox,det_score,blur,embedding,crop_key,excluded_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.execute("UPDATE images SET status='done', content_sha256=?, width=?, height=? WHERE id=?",
                    (sha, w, h, img_row["id"]))
    return kept, excluded


def _run(gid: str, url: str):
    con = db.connect()
    t0 = time.time()
    try:
        # ---- 1. resolve manifest
        _set(con, gid, status="ingesting")
        publish(gid, "status", {"status": "ingesting", "message": "Opening the gallery…"})

        def on_progress(n):
            publish(gid, "progress", {"phase": "harvest", "images_total": n})

        man = adapters.resolve(url, progress=on_progress)
        if not man.images:
            raise RuntimeError("NO_IMAGES_FOUND")
        images = man.images[: int(cfg.limits.max_images)]
        with con:
            con.executemany(
                "INSERT OR IGNORE INTO images (id,gallery_id,source_url,page_url,fallback_url) VALUES (?,?,?,?,?)",
                [(db.new_id(), gid, i["source_url"], i["page_url"], i.get("fallback_url")) for i in images])
        _set(con, gid, adapter=man.adapter, title=man.title, expected_count=man.expected_count,
             completeness_known=int(man.completeness_known), images_total=len(images),
             message="; ".join(man.notes), status="processing")
        log.info("%s manifest: %d images (%s)", gid[:8], len(images), "; ".join(man.notes))
        publish(gid, "status", {"status": "processing", "images_total": len(images),
                                "notes": man.notes})

        # ---- 2. warm the models once, before the fetch pool starts
        models.get_app()

        # ---- 3. fetch in parallel, infer serially
        pending = con.execute(
            "SELECT id, source_url, page_url, fallback_url FROM images WHERE gallery_id=? AND status='pending'",
            (gid,)).fetchall()
        og_cache: dict = {}
        done = failed = faces = excluded = 0
        since_cluster = 0
        headers = {"User-Agent": adapters.UA, "Referer": url}
        with httpx.Client(headers=headers, follow_redirects=True, http2=False) as client, \
                ThreadPoolExecutor(max_workers=int(cfg.limits.fetch_concurrency)) as pool:
            futs = {pool.submit(_fetch_best, client, r, og_cache): r for r in pending}
            for fut in as_completed(futs):
                row = futs[fut]
                try:
                    data, used = fut.result()
                    if used != row["source_url"]:
                        with con:
                            con.execute("UPDATE images SET source_url=? WHERE id=?", (used, row["id"]))
                    k, x = _process_one(con, gid, row, data)
                    faces += k
                    excluded += x
                    since_cluster += k
                    done += 1
                except Exception as e:                  # noqa: BLE001
                    failed += 1
                    code = str(e)[:80] or type(e).__name__
                    with con:
                        con.execute("UPDATE images SET status='failed', error_code=? WHERE id=?",
                                    (code, row["id"]))
                    log.warning("%s image failed: %s (%s)", gid[:8], code, row["source_url"][-40:])
                _set(con, gid, images_done=done, images_failed=failed, faces_found=faces,
                     faces_excluded=excluded)
                publish(gid, "progress", {"phase": "process", "images_total": len(images),
                                          "images_done": done, "images_failed": failed,
                                          "faces_found": faces, "faces_excluded": excluded,
                                          "elapsed": round(time.time() - t0, 1)})
                if since_cluster >= int(cfg.limits.cluster_every_n_faces):
                    since_cluster = 0
                    n = cluster.recluster(con, gid, provisional=True)
                    publish(gid, "clusters", {"people": n, "provisional": True})

        # ---- 4. final, authoritative clustering
        n = cluster.recluster(con, gid, provisional=False)
        status = "ready" if failed == 0 else "partial"
        _set(con, gid, status=status, indexed_at=db.now())
        publish(gid, "clusters", {"people": n, "provisional": False})
        publish(gid, "done", {"status": status, "people": n, "images_done": done,
                              "images_failed": failed, "faces_found": faces,
                              "faces_excluded": excluded, "elapsed": round(time.time() - t0, 1)})
        log.info("%s %s: %d imgs, %d failed, %d faces (+%d excluded), %d people, %.0fs",
                 gid[:8], status, done, failed, faces, excluded, n, time.time() - t0)
    except Exception as e:                              # noqa: BLE001
        code = str(e) if str(e).isupper() else "INDEX_FAILED"
        log.exception("%s failed", gid[:8])
        _set(con, gid, status="failed", failure_code=code, message=str(e)[:300])
        publish(gid, "error", {"error_code": code, "message": str(e)[:300]})
    finally:
        con.close()
        _jobs.pop(gid, None)
