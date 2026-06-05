"""
pipeline/run_pipeline.py
------------------------
Orchestrates the full data pipeline in order:
  1. Ingest from USDA API  → data/foods.db
  2. Deduplicate            → cleans foods table
  3. Tag clinical flags     → sets is_high_fodmap, allergens, etc.
  4. Build Bloom filters    → data/bloom_filters/*.bloom
  5. Build FAISS index      → data/faiss.index + data/faiss_meta.pkl

Run once before starting the app:
    python -m pipeline.run_pipeline --api-key YOUR_USDA_KEY

Get a free USDA API key at: https://fdc.nal.usda.gov/api-guide.html
(Instant, no approval needed)
"""

import argparse
import logging
import time

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main(api_key: str, target_records: int) -> None:
    t0 = time.time()

    log.info("=" * 60)
    log.info("NutriAI Data Pipeline — Starting")
    log.info("=" * 60)

    # Step 1 — Ingest
    log.info("\n[1/5] Ingesting from USDA FoodData Central...")
    from pipeline.ingest import run_ingest
    run_ingest(api_key, target=target_records)

    # Step 2 — Deduplicate
    log.info("\n[2/5] Deduplicating food records...")
    from pipeline.dedupe import run_dedupe
    run_dedupe()

    # Step 3 — Tag clinical constraints
    log.info("\n[3/5] Tagging clinical flags (FODMAP, GERD, allergens, GI)...")
    from filters.tag import run_tagging
    run_tagging()

    # Step 4 — Build Bloom filters
    log.info("\n[4/5] Building Bloom filters...")
    from filters.bloom import build_all_filters
    build_all_filters()

    # Step 5 — Build FAISS index
    log.info("\n[5/5] Building FAISS nutritional embedding index...")
    from pipeline.build_index import build_index
    build_index()

    elapsed = time.time() - t0
    log.info("\n" + "=" * 60)
    log.info(f"Pipeline complete in {elapsed:.1f}s")
    log.info("You can now start the app:  streamlit run app.py")
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NutriAI data pipeline")
    parser.add_argument("--api-key",  required=True, help="USDA FoodData Central API key")
    parser.add_argument("--limit",    type=int, default=12000,
                        help="Target number of food records to ingest (default: 12000)")
    args = parser.parse_args()
    main(args.api_key, args.limit)
    