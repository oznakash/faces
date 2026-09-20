"""FastAPI app. Tech Spec §10 — local profile serves one zero-build page."""
import json
import logging
import queue

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cluster, db, models, pipeline
from .config import CROPS, ROOT, cfg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("faces.server")

app = FastAPI(title="Faces (local)")
db.init()
CROPS.mkdir(parents=True, exist_ok=True)
app.mount("/crops", StaticFiles(directory=CROPS), name="crops")


class SubmitBody(BaseModel):
    url: str


def _gallery_or_404(con, gid):
    r = con.execute("SELECT * FROM galleries WHERE id=?", (gid,)).fetchone()
    if not r:
        raise HTTPException(404, "gallery not found")
    return dict(r)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/galleries")
def submit(body: SubmitBody):
    try:
        gid, cached = pipeline.start(body.url)
    except ValueError as e:
        raise HTTPException(400, {"error_code": str(e), "message": "That doesn't look like a URL."})
    return {"gallery_id": gid, "cached": cached}


@app.get("/api/galleries")
def list_galleries():
    con = db.connect()
    rows = con.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM clusters c WHERE c.gallery_id=g.id) AS people"
        " FROM galleries g ORDER BY created_at DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/galleries/{gid}")
def gallery(gid: str):
    con = db.connect()
    g = _gallery_or_404(con, gid)
    g["people"] = con.execute("SELECT COUNT(*) FROM clusters WHERE gallery_id=?", (gid,)).fetchone()[0]
    con.close()
    return g


@app.get("/api/galleries/{gid}/events")
def events(gid: str):
    con = db.connect()
    g = _gallery_or_404(con, gid)
    con.close()
    q = pipeline.subscribe(gid)

    def gen():
        yield f"event: status\ndata: {json.dumps(g)}\n\n"
        if g["status"] in ("ready", "partial", "failed"):
            yield f"event: done\ndata: {json.dumps(g)}\n\n"
            pipeline.unsubscribe(gid, q)
            return
        try:
            while True:
                try:
                    ev, data = q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {ev}\ndata: {json.dumps(data)}\n\n"
                if ev in ("done", "error"):
                    return
        finally:
            pipeline.unsubscribe(gid, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/galleries/{gid}/people")
def people(gid: str):
    con = db.connect()
    _gallery_or_404(con, gid)
    rows = con.execute(
        "SELECT c.id, c.label, c.face_count, c.image_count, c.provisional, f.crop_key"
        " FROM clusters c LEFT JOIN faces f ON f.id=c.rep_face_id"
        " WHERE c.gallery_id=? ORDER BY c.image_count DESC, c.face_count DESC", (gid,)).fetchall()
    con.close()
    return [{**dict(r), "crop_url": f"/crops/{r['crop_key']}" if r["crop_key"] else None} for r in rows]


@app.get("/api/people/{cid}/photos")
def person_photos(cid: str):
    con = db.connect()
    c = con.execute("SELECT * FROM clusters WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "person not found")
    rows = con.execute(
        "SELECT i.id, i.source_url, i.page_url, i.width, i.height,"
        "       MAX(f.det_score) AS score, COUNT(f.id) AS faces_of_person, f.bbox"
        " FROM faces f JOIN images i ON i.id=f.image_id"
        " WHERE f.cluster_id=? GROUP BY i.id ORDER BY score DESC", (cid,)).fetchall()
    con.close()
    return {"person": dict(c), "photos": [dict(r) for r in rows]}


@app.post("/api/galleries/{gid}/search")
async def search(gid: str, selfie: UploadFile = File(...)):
    """Selfie -> two-tier photo list. The embedding never leaves this request."""
    con = db.connect()
    _gallery_or_404(con, gid)
    data = await selfie.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, {"error_code": "TOO_LARGE", "message": "Max 10 MB."})
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise HTTPException(400, {"error_code": "NOT_AN_IMAGE", "message": "Couldn't read that image."})
    with pipeline._infer_lock:
        found = models.embed_query(arr)
    if not found:
        raise HTTPException(422, {"error_code": "NO_FACE_DETECTED",
                                  "message": "We couldn't find a face in that photo. Try a clearer, front-facing shot."})
    q = found[0]["embedding"]                           # largest face (T-C3 default)
    ids, mat = db.load_embeddings(con, gid)
    if not ids:
        return {"confident": [], "possible": [], "faces_in_selfie": len(found)}
    sims = mat @ (q / (np.linalg.norm(q) + 1e-9))
    meta = {r["id"]: dict(r) for r in con.execute(
        "SELECT f.id, f.image_id, f.bbox, i.source_url, i.page_url FROM faces f"
        " JOIN images i ON i.id=f.image_id WHERE f.gallery_id=? AND f.embedding IS NOT NULL", (gid,))}
    con.close()
    best: dict[str, dict] = {}                         # per image, best-matching face
    for fid, s in zip(ids, sims):
        s = float(s)
        if s < cfg.search.t_possible:
            continue
        m = meta[fid]
        cur = best.get(m["image_id"])
        if cur is None or s > cur["score"]:
            best[m["image_id"]] = {**m, "score": round(s, 3)}
    ranked = sorted(best.values(), key=lambda r: -r["score"])
    return {
        "confident": [r for r in ranked if r["score"] >= cfg.search.t_hit],
        "possible": [r for r in ranked if r["score"] < cfg.search.t_hit],
        "faces_in_selfie": len(found),
        "thresholds": {"t_hit": cfg.search.t_hit, "t_possible": cfg.search.t_possible},
    }


@app.post("/api/galleries/{gid}/recluster")
def recluster(gid: str):
    """Re-run clustering with the current thresholds.yaml — the tuning loop."""
    from . import config
    config.cfg = config.load()
    cluster.cfg = config.cfg
    con = db.connect()
    _gallery_or_404(con, gid)
    n = cluster.recluster(con, gid, provisional=False)
    con.close()
    return {"people": n}


@app.delete("/api/galleries/{gid}")
def delete(gid: str):
    con = db.connect()
    _gallery_or_404(con, gid)
    con.close()
    pipeline.delete(gid)
    return {"deleted": gid}
