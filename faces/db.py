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
  slug               TEXT UNIQUE,              -- short shareable key: /g/{slug}
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
  expires_at         TEXT                      -- NULL = never (local profile). Production sets a TTL.
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

-- Collections: a pool of galleries grouped into one set of people. Faces stay
-- owned by their gallery; a collection only maps them to its own clusters.
CREATE TABLE IF NOT EXISTS collections (
  id          TEXT PRIMARY KEY,
  slug        TEXT UNIQUE,                     -- /c/{slug}
  name        TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  grouped_at  TEXT
);
CREATE TABLE IF NOT EXISTS collection_sources (
  collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  gallery_id    TEXT NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,
  PRIMARY KEY (collection_id, gallery_id)
);
CREATE TABLE IF NOT EXISTS collection_clusters (
  id            TEXT PRIMARY KEY,
  collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  label         TEXT,
  face_count    INTEGER NOT NULL DEFAULT 0,
  image_count   INTEGER NOT NULL DEFAULT 0,
  source_count  INTEGER NOT NULL DEFAULT 0,
  rep_face_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_cclusters ON collection_clusters(collection_id, image_count DESC);
CREATE TABLE IF NOT EXISTS collection_faces (
  collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  face_id       TEXT NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
  cluster_id    TEXT NOT NULL REFERENCES collection_clusters(id) ON DELETE CASCADE,
  PRIMARY KEY (collection_id, face_id)
);
CREATE INDEX IF NOT EXISTS idx_cfaces_cluster ON collection_faces(cluster_id);
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
        cols = {r["name"] for r in con.execute("PRAGMA table_info(galleries)")}
        if "slug" not in cols:                       # migrate a pre-slug database in place
            con.execute("ALTER TABLE galleries ADD COLUMN slug TEXT")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_galleries_slug ON galleries(slug)")
        for r in con.execute("SELECT id FROM galleries WHERE slug IS NULL").fetchall():
            con.execute("UPDATE galleries SET slug=? WHERE id=?", (new_slug(con), r["id"]))
    _relax_expiry(con)
    con.close()


def _relax_expiry(con) -> None:
    """Older local DBs declared expires_at NOT NULL. Rebuild the table so it is
    optional, keeping every row. Children keep their rows: FK enforcement is
    off for the rebuild, and their REFERENCES resolve by name to the new table."""
    info = {r["name"]: r for r in con.execute("PRAGMA table_info(galleries)")}
    if not info["expires_at"]["notnull"]:
        return
    ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS galleries")[1].split(";")[0]
    con.execute("PRAGMA foreign_keys=OFF")
    try:
        with con:
            con.execute("CREATE TABLE galleries_new" + ddl)
            cols = ", ".join(info.keys())
            con.execute(f"INSERT INTO galleries_new ({cols}) SELECT {cols} FROM galleries")
            con.execute("DROP TABLE galleries")
            con.execute("ALTER TABLE galleries_new RENAME TO galleries")
            con.execute("UPDATE galleries SET expires_at=NULL")
    finally:
        con.execute("PRAGMA foreign_keys=ON")


_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"   # no 0/o/1/l — links get read aloud


def new_slug(con, n: int = 6, table: str = "galleries") -> str:
    import secrets
    while True:
        slug = "".join(secrets.choice(_ALPHABET) for _ in range(n))
        if not con.execute(f"SELECT 1 FROM {table} WHERE slug=?", (slug,)).fetchone():
            return slug


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ttl(days: int | None = None) -> str | None:
    """Local profile: indexes never expire and nothing is deleted except on request."""
    if days is None:
        return None
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
