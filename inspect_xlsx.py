import pandas as pd
import sys

path = sys.argv[1]
df = pd.read_excel(path)
print(f"Shape: {df.shape}")
print(f"Columns: {list(df.columns)}")
print("\nDtypes:")
print(df.dtypes)
print("\nFirst 6 rows:")
print(df.head(6).to_string())
print("\nLast 4 rows:")
print(df.tail(4).to_string())
print("\nUnique values in 'VH' (if exists):", df['VH'].unique()[:10] if 'VH' in df.columns else 'N/A')
print("Unique values in 'Team' sample:", df['Team'].unique()[:10] if 'Team' in df.columns else 'N/A')
