# NutriAI — Setup & Run Instructions

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Get a free USDA API key

Visit https://fdc.nal.usda.gov/api-guide.html and sign up — it's instant.

Store it in a `.env` file in the project root:
```
USDA_API_KEY=your_key_here
```

## 3. Run the data pipeline (one-time setup, ~10–15 minutes)

```bash
python -m pipeline.run_pipeline --api-key YOUR_KEY --limit 12000
```

This will:
- Pull 12,000+ food records from USDA into `data/foods.db`
- Deduplicate and clean the data
- Tag clinical flags (FODMAP, GERD, allergens, GI)
- Build Bloom filters for fast constraint checking
- Build a FAISS nutritional embedding index

## 4. Start the app

```bash
streamlit run app.py
```

## Project structure

```
nutriai/
├── app.py                      ← Streamlit UI (build next)
├── requirements.txt
├── data/
│   ├── foods.db                ← SQLite food database (generated)
│   ├── faiss.index             ← FAISS embedding index (generated)
│   ├── faiss_meta.pkl          ← Index metadata (generated)
│   └── bloom_filters/          ← Bloom filter files (generated)
├── pipeline/
│   ├── ingest.py               ← USDA API → SQLite
│   ├── dedupe.py               ← Deduplication & cleaning
│   ├── build_index.py          ← FAISS index builder
│   └── run_pipeline.py         ← Master orchestrator
└── filters/
    ├── tag.py                  ← Clinical constraint tagging
    └── bloom.py                ← Bloom filter build & load
```
