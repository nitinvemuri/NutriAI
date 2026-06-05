import sqlite3
import os
from pathlib import Path
import pandas as pd
DB_PATH = Path(__file__).parent / "data" / "foods.db"

conn = sqlite3.connect(DB_PATH)
df = pd.read_sql("SELECT count(*) from foods", conn)
print(df)

conn.close()