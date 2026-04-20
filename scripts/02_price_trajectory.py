from google.cloud import bigquery
import pandas as pd

client = bigquery.Client(project="ticketcity-tcg")

query = """
SELECT
    i.EventID,
    i.BlockId,
    i.SectionName,
    i.Price,
    i.Quantity,
    i.DataSource,
    i.StockType,
    TIMESTAMP_SECONDS(i.Time)                               AS snapshot_time,
    DATE_DIFF(
        DATE(e.LocalDate),
        DATE(TIMESTAMP_SECONDS(i.Time)),
        DAY
    )                                                       AS days_before_event
FROM `data-ticketcity.TC_Data.InventoryStream` i
JOIN (
    SELECT EventID, LocalDate
    FROM `data-ticketcity.VividIntake.Events`
    WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("2024-09-25")
      AND EventID = 1361816
) e ON i.EventID = e.EventID
WHERE i.EventID = 1361816
ORDER BY i.BlockId, days_before_event DESC
"""

df = client.query(query).to_dataframe()
print(f"Rows: {len(df):,}")
print(f"Sections: {df['SectionName'].nunique()}")
print(f"Days before event range: {df['days_before_event'].min()} to {df['days_before_event'].max()}")
print(f"\nPrice range: ${df['Price'].min()} - ${df['Price'].max()}")
print(f"\nSample:\n{df.head(10)}")

# Save for next steps
df.to_parquet("data/price_trajectory_1361816.parquet")
print("\nSaved to data/price_trajectory_1361816.parquet")