"""Collections: several galleries pooled into one set of people. Tech Spec §16.

Faces are never copied — a collection re-clusters the pooled embeddings of its
source galleries with the same algorithm and thresholds as a single gallery,
and keeps only a mapping face -> collection cluster. Regrouping is seconds.
"""
import logging

import numpy as np

from . import cluster, db
from .config import cfg

log = logging.getLogger("faces.collections")


def _resolve_gallery(con, key: str):
    r = con.execute("SELECT * FROM galleries WHERE id=? OR slug=?", (key, key)).fetchone()
    if not r:
        raise KeyError(f"gallery not found: {key}")
    return r


def get(con, key: str):
    r = con.execute("SELECT * FROM collections WHERE id=? OR slug=?", (key, key)).fetchone()
    return dict(r) if r else None


def sources(con, cid: str) -> list[dict]:
    rows = con.execute(
        "SELECT g.id, g.slug, g.title, g.url_canonical, g.host, g.status, g.images_done, g.faces_found,"
        "       (SELECT COUNT(*) FROM clusters c WHERE c.gallery_id=g.id) AS people, s.position"
        " FROM collection_sources s JOIN galleries g ON g.id=s.gallery_id"
        " WHERE s.collection_id=? ORDER BY s.position", (cid,)).fetchall()
    return [{**dict(r), "short_url": f"/g/{r['slug']}"} for r in rows]


def create(con, name: str, gallery_keys: list[str]) -> dict:
    cid = db.new_id()
    with con:
        con.execute("INSERT INTO collections (id,slug,name,created_at) VALUES (?,?,?,?)",
                    (cid, db.new_slug(con, table="collections"), name.strip(), db.now()))
        for i, k in enumerate(gallery_keys):
            g = _resolve_gallery(con, k)
            con.execute("INSERT OR IGNORE INTO collection_sources (collection_id,gallery_id,position) VALUES (?,?,?)",
                        (cid, g["id"], i))
    regroup(con, cid)
    return get(con, cid)


def add_source(con, cid: str, gallery_key: str) -> None:
    g = _resolve_gallery(con, gallery_key)
    with con:
        pos = con.execute("SELECT COALESCE(MAX(position),-1)+1 FROM collection_sources WHERE collection_id=?",
                          (cid,)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO collection_sources (collection_id,gallery_id,position) VALUES (?,?,?)",
                    (cid, g["id"], pos))
    regroup(con, cid)


def remove_source(con, cid: str, gallery_key: str) -> None:
    g = _resolve_gallery(con, gallery_key)
    with con:
        con.execute("DELETE FROM collection_sources WHERE collection_id=? AND gallery_id=?", (cid, g["id"]))
    regroup(con, cid)


def _sharpness(det_score: float, bbox: str, blur) -> float:
    """"Sharpest face wins, any source": detection confidence, size, and focus,
    each capped so one huge blurry face can't outscore a clean portrait."""
    x1, y1, x2, y2 = map(int, bbox.split(","))
    size = min(1.0, min(x2 - x1, y2 - y1) / 160.0)
    focus = min(1.0, (blur or 0.0) / 200.0)
    return 0.4 * det_score + 0.3 * size + 0.3 * focus


def regroup(con, cid: str) -> int:
    """Re-cluster the pooled faces of every source and rewrite the collection's people."""
    rows = con.execute(
        "SELECT f.id, f.embedding, f.image_id, f.gallery_id, f.det_score, f.blur, f.bbox"
        " FROM faces f JOIN collection_sources s ON s.gallery_id=f.gallery_id"
        " WHERE s.collection_id=? AND f.embedding IS NOT NULL ORDER BY f.gallery_id, f.id", (cid,)).fetchall()
    with con:
        con.execute("DELETE FROM collection_faces WHERE collection_id=?", (cid,))
        con.execute("DELETE FROM collection_clusters WHERE collection_id=?", (cid,))
    if not rows:
        with con:
            con.execute("UPDATE collections SET grouped_at=? WHERE id=?", (db.now(), cid))
        return 0

    ids = [r["id"] for r in rows]
    meta = {r["id"]: r for r in rows}
    mat = np.stack([db.unpack(r["embedding"]) for r in rows]).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    labels = cluster.cluster(mat)
    c = cfg.clustering

    groups: dict[int, list[str]] = {}
    for fid, lab in zip(ids, labels):
        if lab >= 0:
            groups.setdefault(int(lab), []).append(fid)

    kept = 0
    with con:
        for _, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(members) == 1:
                m = meta[members[0]]
                x1, y1, x2, y2 = map(int, m["bbox"].split(","))
                if m["det_score"] < c.singleton_min_det_score or min(x2 - x1, y2 - y1) < c.singleton_min_bbox_px:
                    continue
            rep = max(members, key=lambda f: _sharpness(meta[f]["det_score"], meta[f]["bbox"], meta[f]["blur"]))
            ccid = db.new_id()
            imgs = {meta[f]["image_id"] for f in members}
            srcs = {meta[f]["gallery_id"] for f in members}
            con.execute(
                "INSERT INTO collection_clusters (id,collection_id,label,face_count,image_count,source_count,rep_face_id)"
                " VALUES (?,?,?,?,?,?,?)", (ccid, cid, None, len(members), len(imgs), len(srcs), rep))
            con.executemany("INSERT INTO collection_faces (collection_id,face_id,cluster_id) VALUES (?,?,?)",
                            [(cid, f, ccid) for f in members])
            kept += 1
        for i, row in enumerate(con.execute(
                "SELECT id FROM collection_clusters WHERE collection_id=? ORDER BY image_count DESC, face_count DESC",
                (cid,)).fetchall(), start=1):
            con.execute("UPDATE collection_clusters SET label=? WHERE id=?", (f"Person {i}", row["id"]))
        con.execute("UPDATE collections SET grouped_at=? WHERE id=?", (db.now(), cid))
    log.info("collection %s: %d faces from %d sources -> %d people", cid[:8], len(ids),
             len({r["gallery_id"] for r in rows}), kept)
    return kept


def load_embeddings(con, cid: str):
    rows = con.execute(
        "SELECT f.id, f.embedding FROM faces f JOIN collection_sources s ON s.gallery_id=f.gallery_id"
        " WHERE s.collection_id=? AND f.embedding IS NOT NULL", (cid,)).fetchall()
    if not rows:
        return [], np.zeros((0, 512), dtype=np.float32)
    ids = [r["id"] for r in rows]
    mat = np.stack([db.unpack(r["embedding"]) for r in rows]).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    return ids, mat
