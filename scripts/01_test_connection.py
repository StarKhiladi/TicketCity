from google.cloud import bigquery

client = bigquery.Client(project="ticketcity-tcg")

query = """
SELECT
    EventID,
    COUNT(DISTINCT Time) AS num_snapshots,
    MIN(TIMESTAMP_SECONDS(Time)) AS first_snapshot,
    MAX(TIMESTAMP_SECONDS(Time)) AS last_snapshot
FROM `data-ticketcity.TC_Data.InventoryStream`
WHERE EventID = 1361816
GROUP BY EventID
"""

df = client.query(query).to_dataframe()
print("Connection successful!")
print(df)