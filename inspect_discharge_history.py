import pandas as pd
from sqlalchemy import create_engine, text

engine = create_engine(
    "postgresql://postgres:postgres@localhost:5432/flood_ews"
)

with engine.connect() as conn:
    columns = pd.read_sql(
        text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'dam_discharge_events'
            ORDER BY ordinal_position
        """),
        conn,
    )

    events = pd.read_sql(
        text("SELECT * FROM dam_discharge_events LIMIT 20"),
        conn,
    )

print("TABLE COLUMNS:")
print(columns.to_string(index=False))

print("\nEXISTING DISCHARGE RECORDS:")
print(events.to_string(index=False))

print("\nRows:", len(events))