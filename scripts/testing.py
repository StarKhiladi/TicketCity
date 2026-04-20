from google.cloud import bigquery
import pandas as pd

client = bigquery.Client(project="ticketcity-tcg")

def get_latest_vivid_partition() -> str:
    query = """
    SELECT partition_id
    FROM `data-ticketcity.VividIntake.INFORMATION_SCHEMA.PARTITIONS`
    WHERE table_name = 'Events'
      AND partition_id NOT IN ('__NULL__', '__UNPARTITIONED__')
    ORDER BY partition_id DESC
    LIMIT 1
    """
    rows = list(client.query(query).result())
    if not rows:
        raise RuntimeError("No valid VividIntake.Events partition found.")
    pid = str(rows[0][0])
    return f"{pid[:4]}-{pid[4:6]}-{pid[6:8]}"

PARTITION = get_latest_vivid_partition()

query = f"""
SELECT
    e.EventID,
    e.Name,
    e.LocalDate,
    e.ListingCount,
    e.TicketCount,
    e.MinPrice,
    e.MaxPrice,
    c.Category_Name,
    v.Venue_Name,
    v.City,
    v.State,
    DATE_DIFF(DATE(e.LocalDate), CURRENT_DATE(), DAY) AS days_to_event
FROM `data-ticketcity.VividIntake.Events` e
LEFT JOIN `data-ticketcity.VividIntake.Categories` c
    ON e.CategoryID = c.Category_ID
   AND DATE(c._PARTITIONTIME) = DATE(e._PARTITIONTIME)
LEFT JOIN `data-ticketcity.VividIntake.Venues` v
    ON e.VenueID = v.Venue_ID
   AND DATE(v._PARTITIONTIME) = DATE(e._PARTITIONTIME)
WHERE DATE(e._PARTITIONTIME) = DATE("{PARTITION}")
  AND DATE(e.LocalDate) BETWEEN DATE_ADD(CURRENT_DATE(), INTERVAL 30 DAY)
                            AND DATE_ADD(CURRENT_DATE(), INTERVAL 60 DAY)
  AND e.ListingCount >= 100
  AND e.TicketCount >= 200
  AND e.MinPrice > 10
  AND e.MaxPrice > e.MinPrice
  AND e.Name NOT LIKE '%Parking%'
  AND e.Name NOT LIKE '%Test%'
  AND e.Name NOT LIKE '%Gift Card%'
  AND e.Name NOT LIKE '%eGift%'
  AND e.Name NOT LIKE '%Package%'
  AND e.Name NOT LIKE '%Voucher%'
ORDER BY days_to_event ASC, e.ListingCount DESC
LIMIT 200
"""

df = client.query(query).to_dataframe()
print(df.head(25).to_string(index=False))