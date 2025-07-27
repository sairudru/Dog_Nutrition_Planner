import pandas as pd
from sqlalchemy import create_engine

# Load Excel file
df = pd.read_csv('user_ingredients.csv')  # Add sheet_name= if needed

# Create PostgreSQL connection string
# Format: postgresql://username:password@host:port/database
engine = create_engine('postgresql://postgres:admin@localhost:5432/postgres')

# Write to PostgreSQL (replace 'your_table' with your target table name)
df.to_sql('user_ingredients', engine, if_exists='replace', index=False)

print("Data uploaded successfully!")