"""SQLite mirror of the Postgres schema in Tech Spec §8.

Local profile: vectors are raw float32 BLOBs and search is a numpy scan.
At a few thousand faces a full scan is ~1ms, so pgvector's HNSW index
would buy nothing. Column names match the spec so the port is mechanical.
"""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import DB_PATH, DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS galleries (
  id                 TEXT PRIMARY KEY,
  url_canonical      TEXT NOT NULL UNIQUE,
  host               TEXT,
  adapter            TEXT,
  title              TEXT,
  expected_count     INTEGER,
  completeness_known INTEGER NOT NULL DEFAULT 0,
  status             TEXT NOT NULL,
  failure_code       TEXT,
  message            TEXT,
  images_total       INTEGER NOT NULL DEFAULT 0,
  images_done        INTEGER NOT NULL DEFAULT 0,
  images_failed      INTEGER NOT NULL DEFAULT 0,
  faces_found        INTEGER NOT NULL DEFAULT 0,
  faces_excluded     INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  indexed_at         TEXT,
  expires_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
  id             TEXT PRIMARY KEY,
  gallery_id     TEXT NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  source_url     TEXT NOT NULL,
  page_url       TEXT,
  fallback_url   TEXT,
  content_sha256 TEXT,
  width          INTEGER,
  height         INTEGER,
  status         TEXT NOT NULL DEFAULT 'pending',
  error_code     TEXT,
  UNIQUE (gallery_id, source_url)
);
CREATE INDEX IF NOT EXISTS idx_images_gallery ON images(gallery_id, status);
CREATE INDEX IF NOT EXISTS idx_images_hash ON images(content_sha256);

CREATE TABLE IF NOT EXISTS clusters (
  id          TEXT PRIMARY KEY,
  gallery_id  TEXT NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  label       TEXT,
  face_count  INTEGER NOT NULL DEFAULT 0,
  image_count INTEGER NOT NULL DEFAULT 0,
  rep_face_id TEXT,
  provisional INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_clusters_gallery ON clusters(gallery_id, image_count DESC);

CREATE TABLE IF NOT EXISTS faces (
  id              TEXT PRIMARY KEY,
  gallery_id      TEXT NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  image_id        TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  bbox            TEXT NOT NULL,
  det_score       REAL NOT NULL,
  blur            REAL,
  embedding       BLOB,
  crop_key        TEXT,
  cluster_id      TEXT,
  excluded_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_faces_gallery ON faces(gallery_id, cluster_id);
CREATE INDEX IF NOT EXISTS idx_faces_image ON faces(image_id);
"""


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")       # worker writes while the UI reads
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init() -> None:
    con = connect()
    with con:
        con.executescript(SCHEMA)
    con.close()


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ttl(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def pack(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def load_embeddings(con, gallery_id):
    """All embedded faces for a gallery as (ids, L2-normalized matrix)."""
    rows = con.execute(
        "SELECT id, embedding FROM faces WHERE gallery_id=? AND embedding IS NOT NULL",
        (gallery_id,),
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 512), dtype=np.float32)
    ids = [r["id"] for r in rows]
    mat = np.stack([unpack(r["embedding"]) for r in rows]).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    return ids, mat
