from google.cloud import bigquery
import pandas as pd

client = bigquery.Client(project="ticketcity-tcg")
PARTITION = "2026-04-03"

query = f"""
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
        WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{PARTITION}")
          AND Name NOT LIKE '%Test%'
          AND ListingCount > 5
    ) e
    JOIN (
        SELECT Category_ID, Category_Name
        FROM `data-ticketcity.VividIntake.Categories`
        WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{PARTITION}")
          AND Category_Name IN (
            'NCAA Basketball', 'PGA Golf', 'NFL Football',
            'Rock', 'Pop', 'Broadway', 'Comedy', 'Arts and Theater'
          )
    ) c ON e.CategoryID = c.Category_ID
    JOIN (
        SELECT Venue_ID, Venue_Name, City, State
        FROM `data-ticketcity.VividIntake.Venues`
        WHERE TIMESTAMP_TRUNC(_PARTITIONTIME, DAY) = TIMESTAMP("{PARTITION}")
    ) v ON e.VenueID = v.Venue_ID
    WHERE v.Venue_Name NOT LIKE '%Test%'
)
SELECT EventID, Name, Category_Name, ListingCount,
       MinPrice, MaxPrice, Venue_Name, City, State, LocalDate
FROM ranked
WHERE rank = 1
ORDER BY Category_Name
"""

df = client.query(query).to_dataframe()
print(df.to_string())
df.to_csv("data/one_per_category.csv", index=False)
print("\nSaved to data/one_per_category.csv")