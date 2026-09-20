"""Grouping faces into people. Tech Spec §5.

Threshold graph + centroid consolidation. Over-merging is worse than
over-splitting: showing someone a stranger in "their" photos destroys trust,
so a merge must satisfy BOTH a strong pairwise edge and a centroid check.
"""
import logging

import numpy as np

from . import db
from .config import cfg

log = logging.getLogger("faces.cluster")


class _UF:
    def __init__(self, n):
        self.p = list(range(n))
        self.size = [1] * n

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        self.size[ra] += self.size[rb]
        return True


def _knn(mat: np.ndarray, k: int):
    """Chunked top-k cosine neighbours. Chunking keeps peak memory at O(chunk*N)."""
    n = len(mat)
    k = min(k + 1, n)
    idx = np.zeros((n, k), dtype=np.int32)
    sim = np.zeros((n, k), dtype=np.float32)
    for s in range(0, n, 512):
        e = min(s + 512, n)
        block = mat[s:e] @ mat.T                      # rows are L2-normalized => cosine
        part = np.argpartition(-block, k - 1, axis=1)[:, :k]
        vals = np.take_along_axis(block, part, axis=1)
        order = np.argsort(-vals, axis=1)
        idx[s:e] = np.take_along_axis(part, order, axis=1)
        sim[s:e] = np.take_along_axis(vals, order, axis=1)
    return idx, sim


def cluster(mat: np.ndarray):
    """Returns a label per row (-1 = dropped singleton)."""
    n = len(mat)
    if n == 0:
        return np.zeros(0, dtype=np.int32)
    c = cfg.clustering
    idx, sim = _knn(mat, int(c.knn))

    edges = []
    for i in range(n):
        for j, s in zip(idx[i], sim[i]):
            if j != i and s >= c.t_link:
                a, b = (i, int(j)) if i < j else (int(j), i)
                edges.append((float(s), a, b))
    # Strongest edges first: deterministic, and merges start from the safest evidence.
    edges.sort(key=lambda e: (-e[0], e[1], e[2]))

    uf = _UF(n)
    sums = mat.astype(np.float32).copy()               # running centroid sums per root
    for s, a, b in edges:
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            continue
        ca = sums[ra] / uf.size[ra]
        cb = sums[rb] / uf.size[rb]
        cs = float(np.dot(ca, cb) / ((np.linalg.norm(ca) * np.linalg.norm(cb)) + 1e-9))
        if cs < c.t_merge:
            continue                                    # blocks A~B~C chaining two people together
        total = sums[ra] + sums[rb]
        if uf.union(a, b):
            sums[uf.find(a)] = total

    labels = np.full(n, -1, dtype=np.int32)
    groups = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    nxt = 0
    for _, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[1][0])):
        labels[members] = nxt
        nxt += 1
    return labels


def recluster(con, gallery_id: str, provisional: bool = True) -> int:
    """Re-cluster a gallery and rewrite its clusters table. Deterministic (T-B10)."""
    ids, mat = db.load_embeddings(con, gallery_id)
    if not ids:
        return 0
    labels = cluster(mat)
    c = cfg.clustering

    meta = {r["id"]: r for r in con.execute(
        "SELECT id, image_id, det_score, bbox FROM faces WHERE gallery_id=?", (gallery_id,))}

    groups = {}
    for face_id, lab in zip(ids, labels):
        if lab >= 0:
            groups.setdefault(int(lab), []).append(face_id)

    with con:
        con.execute("DELETE FROM clusters WHERE gallery_id=?", (gallery_id,))
        con.execute("UPDATE faces SET cluster_id=NULL WHERE gallery_id=?", (gallery_id,))
        pos = {fid: i for i, fid in enumerate(ids)}
        kept = 0
        for _, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(members) == 1:
                m = meta[members[0]]
                x1, y1, x2, y2 = map(int, m["bbox"].split(","))
                # A one-appearance attendee earns a slot; a marginal stray face does not.
                if m["det_score"] < c.singleton_min_det_score or \
                   min(x2 - x1, y2 - y1) < c.singleton_min_bbox_px:
                    continue
            sub = mat[[pos[f] for f in members]]
            centroid = sub.mean(axis=0)
            centroid /= np.linalg.norm(centroid) + 1e-9
            scores = sub @ centroid
            rep = max(
                zip(members, scores),
                key=lambda ms: 0.6 * meta[ms[0]]["det_score"] + 0.4 * float(ms[1]),
            )[0]
            cid = db.new_id()
            imgs = {meta[f]["image_id"] for f in members}
            con.execute(
                "INSERT INTO clusters (id,gallery_id,label,face_count,image_count,rep_face_id,provisional)"
                " VALUES (?,?,?,?,?,?,?)",
                (cid, gallery_id, None, len(members), len(imgs), rep, 1 if provisional else 0))
            con.executemany("UPDATE faces SET cluster_id=? WHERE id=?",
                            [(cid, f) for f in members])
            kept += 1

        # Label by prominence so "Person 1" is the most-photographed person.
        for i, row in enumerate(con.execute(
                "SELECT id FROM clusters WHERE gallery_id=? ORDER BY image_count DESC, face_count DESC",
                (gallery_id,)).fetchall(), start=1):
            con.execute("UPDATE clusters SET label=? WHERE id=?", (f"Person {i}", row["id"]))
    log.info("clustered %s: %d faces -> %d people", gallery_id[:8], len(ids), kept)
    return kept
