from pathlib import Path
import yaml

import os

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("FACES_DATA", ROOT / "data"))   # hosted: the persistent volume, e.g. /data
CROPS = DATA / "crops"
DB_PATH = DATA / "faces.db"
MODELS = DATA / "models"


class Config(dict):
    """Dotted access over the YAML so callers read like cfg.search.t_hit."""

    def __getattr__(self, k):
        v = self[k]
        return Config(v) if isinstance(v, dict) else v


def load() -> Config:
    with open(ROOT / "config" / "thresholds.yaml") as f:
        return Config(yaml.safe_load(f))


cfg = load()
