"""
final project 423.py
---------------------
Main entry point for NutriAI data pipeline.
Orchestrates the full data processing workflow.

Usage:
    python "final project 423.py" --api-key YOUR_USDA_KEY [--limit 12000]
    python "final project 423.py" --limit 12000  # if USDA_API_KEY is set in .env

Get a free USDA API key at: https://fdc.nal.usda.gov/api-guide.html
"""

import argparse
import logging
import sys
import time
import os
from pathlib import Path

from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

def get_api_key(cli_api_key: str | None) -> str:
    if cli_api_key and cli_api_key.strip():
        return cli_api_key.strip()

    env_api_key = os.environ.get("USDA_API_KEY")
    if env_api_key and env_api_key.strip():
        return env_api_key.strip()

    raise ValueError(
        "USDA API key is required. Pass --api-key or set USDA_API_KEY in .env"
    )


def main(api_key: str, target_records: int = 12000):
    """
    Run the complete NutriAI data pipeline.
    
    Args:
        api_key: USDA FoodData Central API key
        target_records: Target number of food records to ingest (default: 12000)
    """
    t0 = time.time()
    
    log.info("=" * 70)
    log.info("NutriAI Data Pipeline — Starting")
    log.info("=" * 70)
    
    try:
        # Step 1 — Ingest from USDA API
        log.info("\n[1/5] Ingesting from USDA FoodData Central...")
        from data_pipeline.pipeline.ingest import run_ingest
        run_ingest(api_key, target=target_records)
        log.info("✓ Ingestion complete")
        
        # Step 2 — Deduplicate
        log.info("\n[2/5] Deduplicating food records...")
        from data_pipeline.pipeline.dedupe import run_dedupe
        run_dedupe()
        log.info("✓ Deduplication complete")
        
        # Step 3 — Tag clinical constraints
        log.info("\n[3/5] Tagging clinical flags (FODMAP, GERD, allergens, GI)...")
        from data_pipeline.filters.tag import run_tagging
        run_tagging()
        log.info("✓ Tagging complete")
        
        # Step 4 — Build Bloom filters
        log.info("\n[4/5] Building Bloom filters...")
        from data_pipeline.filters.bloom import build_all_filters
        build_all_filters()
        log.info("✓ Bloom filters built")
        
        # Step 5 — Build FAISS index
        log.info("\n[5/5] Building FAISS nutritional embedding index...")
        from data_pipeline.pipeline.build_index import build_index
        build_index()
        log.info("✓ FAISS index built")
        
        elapsed = time.time() - t0
        log.info("\n" + "=" * 70)
        log.info(f"✓ Pipeline complete in {elapsed:.1f}s")
        log.info("=" * 70)
        log.info("\nNext steps:")
        log.info("  1. Check data_pipeline/data/ for generated files:")
        log.info("     - foods.db (SQLite database)")
        log.info("     - faiss.index (FAISS embedding index)")
        log.info("     - bloom_filters/ (Bloom filter files)")
        log.info("  2. Start your app with: streamlit run app.py")
        
        return 0
        
    except Exception as e:
        log.error(f"✗ Pipeline failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NutriAI Data Pipeline - Process food data from USDA FDC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python "final project 423.py" --api-key abc123xyz789
  python "final project 423.py" --api-key abc123xyz789 --limit 5000
        """
    )
    
    parser.add_argument(
        "--api-key",
        required=False,
        help="USDA FoodData Central API key (or set USDA_API_KEY in .env)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=12000,
        help="Target number of food records to ingest (default: 12000)"
    )
    
    args = parser.parse_args()
    
    try:
        api_key = get_api_key(args.api_key)
    except ValueError as e:
        log.error(f"Error: {e}")
        sys.exit(1)

    # Run pipeline
    exit_code = main(api_key, args.limit)
    sys.exit(exit_code)
