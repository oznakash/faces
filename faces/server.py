"""FastAPI app. Tech Spec §10 — local profile serves one zero-build page.

Every per-gallery route accepts either the uuid or the short slug (/g/{slug}).
"""
import io
import json
import logging
import os
import queue
import secrets
import shutil
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from pydantic import BaseModel

from . import adapters, cluster, collections, db, models, pipeline
from .config import CROPS, DATA, DB_PATH, ROOT, cfg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("faces.server")


@asynccontextmanager
async def lifespan(_app):
    db.init()
    CROPS.mkdir(parents=True, exist_ok=True)
    n = pipeline.resume_orphans()                   # jobs interrupted by a restart pick up where they were
    if n:
        log.info("resumed %d interrupted job(s)", n)
    yield


app = FastAPI(title="Faces (local)", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)          # JSON for 800 people is ~120 KB raw, ~20 KB gzipped
app.mount("/crops", StaticFiles(directory=CROPS, check_dir=False), name="crops")


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/crops/"):                 # crops are content-addressed by uuid: never change
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# In-memory embedding pools. Loading 11k vectors from SQLite is ~150 ms per search;
# keeping the matrix warm makes a search cost the inference and nothing else.
# Keyed by a version stamp so any re-index or regroup invalidates it.
_pools: dict[tuple, tuple] = {}


def _pool(kind: str, ident: str, version: str, loader):
    key = (kind, ident)
    hit = _pools.get(key)
    if hit and hit[0] == version:
        return hit[1]
    data = loader()
    _pools[key] = (version, data)
    return data


def _rank(sims, ids, meta):
    """Best-matching face per image, two tiers. Shared by gallery and collection search."""
    best: dict[str, dict] = {}
    t_possible, t_hit = cfg.search.t_possible, cfg.search.t_hit
    for fid, sim in zip(ids, sims.tolist()):
        if sim < t_possible:
            continue
        m = meta[fid]
        cur = best.get(m["image_id"])
        if cur is None or sim > cur["score"]:
            best[m["image_id"]] = {**m, "score": round(sim, 3)}
    ranked = sorted(best.values(), key=lambda r: -r["score"])
    return {"confident": [r for r in ranked if r["score"] >= t_hit],
            "possible": [r for r in ranked if r["score"] < t_hit],
            "thresholds": {"t_hit": t_hit, "t_possible": t_possible}}


def _with_thumbs(rows):
    return [{**r, "thumb_url": adapters.thumb_url(r["source_url"], r.get("fallback_url"))} for r in rows]
INDEX = ROOT / "static" / "index.html"
NO_CACHE = {"Cache-Control": "no-cache"}         # the page must never be stale after a redesign
# Standalone: expose exactly one collection. "/" goes there; the working home lives at /admin.
STANDALONE = os.environ.get("FACES_STANDALONE", "").strip() or None
# Hosted: set FACES_ADMIN_TOKEN and every write route + /admin need it. Indexing is off
# unless FACES_ALLOW_INDEX=1 — the server is a search box; galleries are indexed locally
# and published to it with publish.sh.
ADMIN_TOKEN = os.environ.get("FACES_ADMIN_TOKEN", "").strip() or None
ALLOW_INDEX = os.environ.get("FACES_ALLOW_INDEX", "").strip() == "1" or ADMIN_TOKEN is None


def require_admin(authorization: str | None = Header(default=None)):
    """No token configured (local) -> open. Token configured (hosted) -> Bearer required."""
    if ADMIN_TOKEN is None:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not given or not secrets.compare_digest(given, ADMIN_TOKEN):
        raise HTTPException(401, {"error_code": "UNAUTHORIZED", "message": "Admin token required."})


class _Bucket:
    """Per-IP token bucket for the one public write: selfie search."""
    def __init__(self, rate_per_min: int, burst: int):
        self.rate, self.burst, self.state, self.lock = rate_per_min / 60.0, burst, {}, threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.state.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.state[key] = (tokens, now)
                return False
            self.state[key] = (tokens - 1, now)
            if len(self.state) > 10000:                  # forget the oldest on abuse
                for k in list(self.state)[:5000]:
                    self.state.pop(k, None)
            return True


SEARCH_BUCKET = _Bucket(rate_per_min=20, burst=8)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else request.client.host) if request.client or fwd else "?"


def _rate_limit(request: Request):
    if not SEARCH_BUCKET.allow(_client_ip(request)):
        raise HTTPException(429, {"error_code": "RATE_LIMITED", "message": "Too many searches — try again in a minute."},
                            headers={"Retry-After": "60"})


class SubmitBody(BaseModel):
    url: str


def _gallery_or_404(con, key: str) -> dict:
    """uuid or slug -> gallery row, with short_url filled in."""
    r = con.execute("SELECT * FROM galleries WHERE id=? OR slug=?", (key, key)).fetchone()
    if not r:
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "No indexed gallery at that link."})
    g = dict(r)
    g["short_url"] = f"/g/{g['slug']}"
    return g


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    if STANDALONE:
        con = db.connect()
        exists = collections.get(con, STANDALONE) is not None
        con.close()
        if exists:
            return RedirectResponse(f"/c/{STANDALONE}", status_code=302)
        return FileResponse(ROOT / "static" / "empty.html", headers=NO_CACHE)
    return FileResponse(INDEX, headers=NO_CACHE)


@app.get("/admin")
def admin():
    """The working home: index galleries, build collections. Not linked from anywhere public.
    Hosted: the page loads, but every call it makes needs the admin token (entered once)."""
    return FileResponse(INDEX, headers=NO_CACHE)


@app.get("/api/config")
def client_config():
    """What the page needs to know: whether admin calls need a token, whether indexing is on."""
    return {"hosted": ADMIN_TOKEN is not None, "allow_index": ALLOW_INDEX, "standalone": STANDALONE}


@app.get("/g/{slug}")
def gallery_page(slug: str):
    """Shareable short link — same page, opened straight to this gallery's face wall."""
    return FileResponse(INDEX, headers=NO_CACHE)


@app.get("/c/{slug}")
def collection_page(slug: str):
    """A collection's shareable link: pooled face wall + selfie search, no indexing."""
    return FileResponse(INDEX, headers=NO_CACHE)


# ---------------------------------------------------------------- galleries
@app.post("/api/galleries", dependencies=[Depends(require_admin)])
def submit(body: SubmitBody):
    if not ALLOW_INDEX:
        raise HTTPException(403, {"error_code": "INDEXING_DISABLED",
                                  "message": "This server doesn't index. Index locally and publish."})
    try:
        gid, cached = pipeline.start(body.url)
    except ValueError as e:
        raise HTTPException(400, {"error_code": str(e), "message": "That doesn't look like a URL."})
    con = db.connect()
    g = _gallery_or_404(con, gid)
    con.close()
    return {"gallery_id": gid, "slug": g["slug"], "short_url": g["short_url"], "cached": cached}


@app.get("/api/galleries", dependencies=[Depends(require_admin)])
def list_galleries():
    con = db.connect()
    rows = con.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM clusters c WHERE c.gallery_id=g.id) AS people"
        " FROM galleries g ORDER BY created_at DESC").fetchall()
    con.close()
    return [{**dict(r), "short_url": f"/g/{r['slug']}"} for r in rows]


@app.get("/api/galleries/{key}")
def gallery(key: str):
    con = db.connect()
    g = _gallery_or_404(con, key)
    g["people"] = con.execute("SELECT COUNT(*) FROM clusters WHERE gallery_id=?", (g["id"],)).fetchone()[0]
    con.close()
    return g


@app.get("/api/galleries/{key}/events")
def events(key: str):
    con = db.connect()
    g = _gallery_or_404(con, key)
    g["people"] = con.execute("SELECT COUNT(*) FROM clusters WHERE gallery_id=?", (g["id"],)).fetchone()[0]
    con.close()
    gid = g["id"]
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


@app.get("/api/galleries/{key}/people")
def people(key: str):
    con = db.connect()
    gid = _gallery_or_404(con, key)["id"]
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
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "person not found"})
    rows = con.execute(
        "SELECT i.id, i.source_url, i.fallback_url, i.page_url, i.width, i.height,"
        "       MAX(f.det_score) AS score, COUNT(f.id) AS faces_of_person"
        " FROM faces f JOIN images i ON i.id=f.image_id"
        " WHERE f.cluster_id=? GROUP BY i.id ORDER BY score DESC", (cid,)).fetchall()
    con.close()
    return {"person": dict(c), "photos": _with_thumbs([dict(r) for r in rows])}


@app.post("/api/galleries/{key}/search", dependencies=[Depends(_rate_limit)])
async def search(key: str, selfie: UploadFile = File(...)):
    """Selfie -> two-tier photo list. The embedding never leaves this request."""
    con = db.connect()
    gid = _gallery_or_404(con, key)["id"]
    g = _gallery_or_404(con, key)
    found = _selfie_embedding(await selfie.read())
    q = found[0]["embedding"]                           # largest face (T-C3 default)

    def load():
        ids, mat = db.load_embeddings(con, gid)
        meta = {r["id"]: dict(r) for r in con.execute(
            "SELECT f.id, f.image_id, i.source_url, i.fallback_url, i.page_url FROM faces f"
            " JOIN images i ON i.id=f.image_id WHERE f.gallery_id=? AND f.embedding IS NOT NULL", (gid,))}
        return ids, mat, meta
    ids, mat, meta = _pool("gallery", gid, f"{g['indexed_at']}:{g['faces_found']}", load)
    con.close()
    if not ids:
        return {"confident": [], "possible": [], "faces_in_selfie": len(found)}
    out = _rank(mat @ (q / (np.linalg.norm(q) + 1e-9)), ids, meta)
    out["confident"], out["possible"] = _with_thumbs(out["confident"]), _with_thumbs(out["possible"])
    out["faces_in_selfie"] = len(found)
    return out


@app.post("/api/galleries/{key}/recluster", dependencies=[Depends(require_admin)])
def recluster(key: str):
    """Re-run clustering with the current thresholds.yaml — the tuning loop."""
    from . import config
    config.cfg = config.load()
    cluster.cfg = config.cfg
    con = db.connect()
    gid = _gallery_or_404(con, key)["id"]
    n = cluster.recluster(con, gid, provisional=False)
    con.close()
    return {"people": n}


@app.delete("/api/galleries/{key}", dependencies=[Depends(require_admin)])
def delete(key: str):
    """The only way an index is ever removed: an explicit request."""
    con = db.connect()
    gid = _gallery_or_404(con, key)["id"]
    con.close()
    pipeline.delete(gid)
    return {"deleted": gid}


# ---------------------------------------------------------------- collections
class CollectionBody(BaseModel):
    name: str
    galleries: list[str] = []
    slug: str | None = None                     # optional link name, e.g. "iia-summit-2026"


class CollectionPatch(BaseModel):
    name: str | None = None
    slug: str | None = None


SLUG_MESSAGES = {
    "SLUG_INVALID": "Link names use lowercase letters, digits and hyphens (2–64 characters).",
    "SLUG_TAKEN": "That link name is already used by another collection.",
}


class SourceBody(BaseModel):
    gallery: str


def _collection_or_404(con, key: str) -> dict:
    c = collections.get(con, key)
    if not c:
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "No collection at that link."})
    c["short_url"] = f"/c/{c['slug']}"
    c["sources"] = collections.sources(con, c["id"])
    c["people"] = con.execute("SELECT COUNT(*) FROM collection_clusters WHERE collection_id=?",
                              (c["id"],)).fetchone()[0]
    c["faces"] = con.execute("SELECT COUNT(*) FROM collection_faces WHERE collection_id=?",
                             (c["id"],)).fetchone()[0]
    return c


def _decode_upload(data: bytes) -> np.ndarray:
    """Phone photos arrive rotated-by-EXIF and often as HEIC; decode through PIL
    so orientation is applied and HEIC works, then hand OpenCV an upright BGR frame."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif                        # optional; registers HEIC/HEIF with PIL
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:                             # noqa: BLE001
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise HTTPException(400, {"error_code": "NOT_AN_IMAGE", "message": "Couldn't read that image."})
        return arr
    arr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    h, w = arr.shape[:2]
    if max(h, w) > 2000:                          # phone originals are 4000px+; nothing gained past this
        sc = 2000 / max(h, w)
        arr = cv2.resize(arr, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
    return arr


def _selfie_embedding(data: bytes):
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, {"error_code": "TOO_LARGE", "message": "Max 10 MB."})
    arr = _decode_upload(data)
    with pipeline._infer_lock:
        found = models.embed_query(arr)
    if not found:
        raise HTTPException(422, {"error_code": "NO_FACE_DETECTED",
                                  "message": "We couldn't find a face in that photo. Try a clearer, front-facing shot."})
    return found


@app.post("/api/collections", dependencies=[Depends(require_admin)])
def create_collection(body: CollectionBody):
    if not body.name.strip():
        raise HTTPException(400, {"error_code": "INVALID", "message": "Give the collection a name."})
    con = db.connect()
    try:
        c = collections.create(con, body.name, body.galleries, body.slug)
    except KeyError as e:
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": str(e)})
    except ValueError as e:
        raise HTTPException(400, {"error_code": str(e), "message": SLUG_MESSAGES.get(str(e), str(e))})
    out = _collection_or_404(con, c["id"])
    con.close()
    return out


@app.get("/api/collections", dependencies=[Depends(require_admin)])
def list_collections():
    con = db.connect()
    rows = con.execute("SELECT id FROM collections ORDER BY created_at DESC").fetchall()
    out = [_collection_or_404(con, r["id"]) for r in rows]
    con.close()
    return out


@app.get("/api/collections/{key}")
def collection(key: str):
    con = db.connect()
    c = _collection_or_404(con, key)
    con.close()
    return c


@app.patch("/api/collections/{key}", dependencies=[Depends(require_admin)])
def patch_collection(key: str, body: CollectionPatch):
    """Rename a collection or change its link name. The old link stops working."""
    con = db.connect()
    c = _collection_or_404(con, key)
    try:
        if body.slug is not None:
            collections.set_slug(con, c["id"], body.slug)
        if body.name is not None and body.name.strip():
            with con:
                con.execute("UPDATE collections SET name=? WHERE id=?", (body.name.strip(), c["id"]))
    except ValueError as e:
        raise HTTPException(400, {"error_code": str(e), "message": SLUG_MESSAGES.get(str(e), str(e))})
    out = _collection_or_404(con, c["id"])
    con.close()
    return out


@app.post("/api/collections/{key}/sources", dependencies=[Depends(require_admin)])
def add_collection_source(key: str, body: SourceBody):
    con = db.connect()
    c = _collection_or_404(con, key)
    try:
        collections.add_source(con, c["id"], body.gallery)
    except KeyError as e:
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": str(e)})
    out = _collection_or_404(con, c["id"])
    con.close()
    return out


@app.delete("/api/collections/{key}/sources/{gallery}", dependencies=[Depends(require_admin)])
def remove_collection_source(key: str, gallery: str):
    con = db.connect()
    c = _collection_or_404(con, key)
    collections.remove_source(con, c["id"], gallery)
    out = _collection_or_404(con, c["id"])
    con.close()
    return out


@app.post("/api/collections/{key}/regroup", dependencies=[Depends(require_admin)])
def regroup_collection(key: str):
    from . import config
    config.cfg = config.load()
    cluster.cfg = config.cfg
    con = db.connect()
    c = _collection_or_404(con, key)
    n = collections.regroup(con, c["id"])
    con.close()
    return {"people": n}


@app.get("/api/collections/{key}/people")
def collection_people(key: str):
    con = db.connect()
    c = _collection_or_404(con, key)
    rows = con.execute(
        "SELECT cc.id, cc.label, cc.face_count, cc.image_count, cc.source_count, f.crop_key, g.slug AS rep_source"
        " FROM collection_clusters cc LEFT JOIN faces f ON f.id=cc.rep_face_id"
        " LEFT JOIN galleries g ON g.id=f.gallery_id"
        " WHERE cc.collection_id=? ORDER BY cc.image_count DESC, cc.face_count DESC", (c["id"],)).fetchall()
    con.close()
    return [{**dict(r), "crop_url": f"/crops/{r['crop_key']}" if r["crop_key"] else None} for r in rows]


@app.get("/api/collections/{key}/people/{ccid}/photos")
def collection_person_photos(key: str, ccid: str):
    con = db.connect()
    c = _collection_or_404(con, key)
    person = con.execute("SELECT * FROM collection_clusters WHERE id=? AND collection_id=?",
                         (ccid, c["id"])).fetchone()
    if not person:
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": "person not found"})
    rows = con.execute(
        "SELECT i.id, i.source_url, i.fallback_url, i.page_url, MAX(f.det_score) AS score,"
        "       g.slug AS source_slug, g.title AS source_title"
        " FROM collection_faces cf JOIN faces f ON f.id=cf.face_id"
        " JOIN images i ON i.id=f.image_id JOIN galleries g ON g.id=i.gallery_id"
        " WHERE cf.cluster_id=? GROUP BY i.id ORDER BY score DESC", (ccid,)).fetchall()
    con.close()
    return {"person": dict(person), "photos": _with_thumbs([dict(r) for r in rows])}


@app.post("/api/collections/{key}/search", dependencies=[Depends(_rate_limit)])
async def collection_search(key: str, selfie: UploadFile = File(...)):
    """Selfie against the pooled faces of every source. Results carry their source."""
    con = db.connect()
    c = _collection_or_404(con, key)
    found = _selfie_embedding(await selfie.read())
    q = found[0]["embedding"]

    def load():
        ids, mat = collections.load_embeddings(con, c["id"])
        meta = {r["id"]: dict(r) for r in con.execute(
            "SELECT f.id, f.image_id, i.source_url, i.fallback_url, i.page_url, g.slug AS source_slug, g.title AS source_title"
            " FROM faces f JOIN images i ON i.id=f.image_id JOIN galleries g ON g.id=i.gallery_id"
            " JOIN collection_sources s ON s.gallery_id=g.id"
            " WHERE s.collection_id=? AND f.embedding IS NOT NULL", (c["id"],))}
        return ids, mat, meta
    ids, mat, meta = _pool("collection", c["id"], f"{c['grouped_at']}:{c['faces']}", load)
    con.close()
    if not ids:
        return {"confident": [], "possible": [], "faces_in_selfie": len(found)}
    out = _rank(mat @ (q / (np.linalg.norm(q) + 1e-9)), ids, meta)
    out["confident"], out["possible"] = _with_thumbs(out["confident"]), _with_thumbs(out["possible"])
    out["faces_in_selfie"] = len(found)
    return out


# ---------------------------------------------------------------- publish (hosted)
@app.post("/api/admin/import", dependencies=[Depends(require_admin)])
async def import_data(bundle: UploadFile = File(...)):
    """Replace this server's index with a bundle from publish.sh: faces.db + crops/.
    Written to a temp dir, validated, then swapped in atomically. Existing data
    is kept as data.prev until the next import, so a bad publish is one rename away."""
    tmp = Path(tempfile.mkdtemp(prefix="faces-import-", dir=DATA.parent))
    try:
        bundle_path = tmp / "bundle.tar.gz"
        with open(bundle_path, "wb") as f:
            while chunk := await bundle.read(1 << 20):
                f.write(chunk)
        with tarfile.open(bundle_path) as tar:
            members = tar.getmembers()
            for m in members:                         # no path escapes, no links
                if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts or m.issym() or m.islnk():
                    raise HTTPException(400, {"error_code": "BAD_BUNDLE", "message": f"unsafe path: {m.name}"})
            names = {m.name for m in members}
            if "faces.db" not in names:
                raise HTTPException(400, {"error_code": "BAD_BUNDLE", "message": "bundle has no faces.db"})
            tar.extractall(tmp / "data")
        # sanity: it must open and have the tables we expect
        import sqlite3
        con = sqlite3.connect(tmp / "data" / "faces.db")
        n_g = con.execute("SELECT COUNT(*) FROM galleries").fetchone()[0]
        n_c = con.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        n_f = con.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        con.close()
        # swap: keep models (big, unchanged) in place; replace db + crops
        with pipeline._infer_lock:                    # no search mid-swap
            prev = DATA.parent / "data.prev"
            shutil.rmtree(prev, ignore_errors=True)
            prev.mkdir()
            for name in ("faces.db", "faces.db-wal", "faces.db-shm", "crops"):
                src = DATA / name
                if src.exists():
                    shutil.move(str(src), str(prev / name))
            shutil.move(str(tmp / "data" / "faces.db"), str(DB_PATH))
            if (tmp / "data" / "crops").exists():
                shutil.move(str(tmp / "data" / "crops"), str(CROPS))
            CROPS.mkdir(parents=True, exist_ok=True)
            _pools.clear()
        db.init()
        log.info("imported bundle: %d galleries, %d collections, %d faces", n_g, n_c, n_f)
        return {"galleries": n_g, "collections": n_c, "faces": n_f}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
