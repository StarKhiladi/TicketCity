from google.cloud import bigquery
import pandas as pd
from datetime import datetime, timezone

client = bigquery.Client(project="ticketcity-tcg")

# Find the most recent VividIntake partition that actually has data.
# INFORMATION_SCHEMA.PARTITIONS is a free metadata query — no table scan.
_part_result = client.query("""
    SELECT partition_id
    FROM `data-ticketcity.VividIntake.INFORMATION_SCHEMA.PARTITIONS`
    WHERE table_name = 'Events'
      AND partition_id NOT IN ('__NULL__', '__UNPARTITIONED__')
    ORDER BY partition_id DESC
    LIMIT 1
""").result()

_part_row = next(iter(_part_result), None)
if _part_row:
    # partition_id is YYYYMMDD — convert to YYYY-MM-DD
    _pid = str(_part_row[0])
    partition_date = f"{_pid[:4]}-{_pid[4:6]}-{_pid[6:8]}"
else:
    # Fallback: yesterday (today's partition may not be loaded yet)
    from datetime import timedelta
    partition_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')

print(f"Using partition: {partition_date}")

query = f"""
SELECT
    e.EventID,
    e.Name,
    e.LocalDate,
    e.ListingCount,
    e.MinPrice,
    e.MaxPrice,
    c.Category_Name,
    v.Venue_Name,
    v.City,
    v.State
FROM (
    SELECT EventID, Name, LocalDate, ListingCount,
           MinPrice, MaxPrice, CategoryID, VenueID
    FROM `data-ticketcity.VividIntake.Events`
    WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{partition_date}")
      AND LocalDate > DATETIME("2026-04-08")
      AND ListingCount > 50
      AND Name NOT LIKE '%Test%'
      AND Name NOT LIKE '%arking%'
      AND IsDateTBD = FALSE
) e
JOIN (
    SELECT Category_ID, Category_Name
    FROM `data-ticketcity.VividIntake.Categories`
    WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{partition_date}")
      AND Category_Name IN (
        'NFL Football', 'NCAA Basketball', 'NBA Basketball',
        'MLB Baseball', 'NHL Hockey', 'PGA Golf',
        'Rock', 'Pop', 'Country and Folk', 'Rap/Hip Hop',
        'Broadway', 'Comedy', 'Arts and Theater'
      )
) c ON e.CategoryID = c.Category_ID
JOIN (
    SELECT Venue_ID, Venue_Name, City, State
    FROM `data-ticketcity.VividIntake.Venues`
    WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{partition_date}")
) v ON e.VenueID = v.Venue_ID
ORDER BY c.Category_Name, e.ListingCount DESC
LIMIT 3
"""

category_query = f"""
WITH ranked AS (
    SELECT
        e.EventID,
        e.Name,
        e.LocalDate,
        e.ListingCount,
        e.MinPrice,
        e.MaxPrice,
        c.Category_Name,
        v.Venue_Name,
        v.City,
        v.State,
        ROW_NUMBER() OVER (
            PARTITION BY c.Category_Name
            ORDER BY e.ListingCount DESC
        ) AS rank
    FROM (
        SELECT EventID, Name, LocalDate, ListingCount,
               MinPrice, MaxPrice, CategoryID, VenueID
        FROM `data-ticketcity.VividIntake.Events`
        WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{partition_date}")
          AND LocalDate > DATETIME("2026-04-08")
          AND ListingCount > 50
          AND Name NOT LIKE '%Test%'
          AND Name NOT LIKE '%arking%'
          AND IsDateTBD = FALSE
    ) e
    JOIN (
        SELECT Category_ID, Category_Name
        FROM `data-ticketcity.VividIntake.Categories`
        WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{partition_date}")
          AND Category_Name IN (
            'NFL Football', 'NCAA Basketball', 'NBA Basketball',
            'MLB Baseball', 'NHL Hockey', 'PGA Golf',
            'Rock', 'Pop', 'Country and Folk', 'Rap/Hip Hop',
            'Broadway', 'Comedy', 'Arts and Theater'
          )
    ) c ON e.CategoryID = c.Category_ID
    JOIN (
        SELECT Venue_ID, Venue_Name, City, State
        FROM `data-ticketcity.VividIntake.Venues`
        WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{partition_date}")
    ) v ON e.VenueID = v.Venue_ID
)
SELECT EventID, Name, LocalDate, ListingCount,
       MinPrice, MaxPrice, Category_Name, Venue_Name, City, State
FROM ranked
WHERE rank <= 3
ORDER BY Category_Name, ListingCount DESC
"""

df = client.query(category_query).to_dataframe()
print(f"\nFuture events with inventory by category:\n")
print(df[['EventID','Name','Category_Name',
          'ListingCount','MinPrice','City',
          'LocalDate']].to_string())
df.to_csv("data/future_test_events.csv", index=False)
print(f"\nSaved to data/future_test_events.csv")