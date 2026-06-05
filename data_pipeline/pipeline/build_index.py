"""
pipeline/build_index.py
-----------------------
Builds a FAISS index over food nutritional vectors.
Each food is embedded as a normalized vector of its nutrient profile.
At query time, FAISS finds nutritionally similar (or dissimilar) foods fast.
 
Install dependencies:
    pip install faiss-cpu numpy
 
Usage:
    python -m pipeline.build_index
 
Output:
    data/faiss.index      — the FAISS flat index
    data/faiss_meta.pkl   — fdc_id + description for each vector position
"""
 
import logging
import pickle
import sqlite3
from pathlib import Path
 
import numpy as np
 
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
 
try:
    import faiss
except ImportError:
    raise ImportError("Run: pip install faiss-cpu")
 
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "foods.db"
INDEX_PATH = Path(__file__).resolve().parents[2] / "data" / "faiss.index"
META_PATH = Path(__file__).resolve().parents[2] / "data" / "faiss_meta.pkl"
 
# Nutrient columns used as embedding dimensions
# Order matters — keep consistent between build and query
EMBED_COLS = [
    "calories", "protein", "carbs", "fat", "fiber",
    "iron", "calcium", "vitamin_b12", "vitamin_d", "zinc",
    "sodium", "potassium", "magnesium",
]
DIM = len(EMBED_COLS)  # 13-dimensional nutritional embedding
 
 
def load_foods(conn: sqlite3.Connection) -> tuple[list[dict], np.ndarray]:
    """Load all foods with complete enough data to embed."""
    # Require at least calories + protein to be non-NULL
    rows = conn.execute(f"""
        SELECT fdc_id, description, food_category,
               {', '.join(EMBED_COLS)},
               is_high_fodmap, is_gerd_trigger, allergens, glycemic_index
        FROM foods
        WHERE calories IS NOT NULL AND protein IS NOT NULL
    """).fetchall()
 
    col_names = ["fdc_id", "description", "food_category"] + EMBED_COLS + \
                ["is_high_fodmap", "is_gerd_trigger", "allergens", "glycemic_index"]
 
    metadata = []
    vectors  = []
 
    for row in rows:
        record = dict(zip(col_names, row))
        vec = np.array(
            [float(record.get(c) or 0.0) for c in EMBED_COLS],
            dtype=np.float32
        )
        metadata.append(record)
        vectors.append(vec)
 
    matrix = np.stack(vectors)
    return metadata, matrix
 
 
def normalize_matrix(matrix: np.ndarray) -> np.ndarray:
    """
    Per-column min-max normalization so no single nutrient dominates the embedding.
    Calories (0–900) would swamp zinc (0–15) without this step.
    """
    col_min = matrix.min(axis=0)
    col_max = matrix.max(axis=0)
    col_range = np.where(col_max - col_min == 0, 1.0, col_max - col_min)
    normalized = (matrix - col_min) / col_range
    return normalized.astype(np.float32), col_min, col_max
 
 
def build_faiss_index(matrix: np.ndarray) -> faiss.Index:
    """
    Flat L2 index — exact nearest-neighbor search.
    For 10k–15k foods this is fast enough (<5ms per query).
    Switch to IndexIVFFlat for 100k+ records.
    """
    index = faiss.IndexFlatL2(DIM)
    index.add(matrix)
    log.info(f"FAISS index built: {index.ntotal:,} vectors, dim={DIM}")
    return index
 
 
def build_index() -> None:
    conn = sqlite3.connect(DB_PATH)
    log.info("Loading foods from DB...")
    metadata, matrix = load_foods(conn)
    conn.close()
 
    log.info(f"Loaded {len(metadata):,} embeddable foods")
    normalized, col_min, col_max = normalize_matrix(matrix)
 
    index = build_faiss_index(normalized)
 
    faiss.write_index(index, str(INDEX_PATH))
    log.info(f"FAISS index saved → {INDEX_PATH}")
 
    with open(META_PATH, "wb") as f:
        pickle.dump({
            "metadata": metadata,
            "col_min":  col_min,
            "col_max":  col_max,
            "embed_cols": EMBED_COLS,
        }, f)
    log.info(f"Metadata saved → {META_PATH}")
 
 
# ── Query helpers (used by the meal planner at runtime) ──────────────────────
 
def load_index() -> tuple[faiss.Index, dict]:
    index = faiss.read_index(str(INDEX_PATH))
    with open(META_PATH, "rb") as f:
        meta = pickle.load(f)
    return index, meta
 
 
def query_similar(index, meta: dict, query_vec: np.ndarray, k: int = 20) -> list[dict]:
    """Return the k most nutritionally similar foods to query_vec."""
    col_min  = meta["col_min"]
    col_max  = meta["col_max"]
    col_range = np.where(col_max - col_min == 0, 1.0, col_max - col_min)
    norm_q   = ((query_vec - col_min) / col_range).astype(np.float32).reshape(1, -1)
 
    distances, indices = index.search(norm_q, k)
    return [meta["metadata"][i] for i in indices[0] if i >= 0]
 
 
if __name__ == "__main__":
    build_index()