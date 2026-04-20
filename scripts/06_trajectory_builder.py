from google.cloud import bigquery
import pandas as pd
import os
import sys
from datetime import datetime, timezone
import importlib.util

client = bigquery.Client(project="ticketcity-tcg")
TODAY = datetime.now(timezone.utc).strftime('%Y-%m-%d')

# ── Import comp engine ───────────────────────────────────────────── #
spec = importlib.util.spec_from_file_location(
    "comp_engine",
    os.path.join(os.path.dirname(__file__), "04_comp.py")
)
comp_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comp_engine)
find_comps       = comp_engine.find_comps
get_event_profile = comp_engine.get_event_profile

# ================================================================== #
#  STEP 1: PULL DAILY TRAJECTORY FROM 75_Event_Daily                  #
# ================================================================== #
def pull_daily_trajectories(comp_event_ids: list) -> pd.DataFrame:
    """
    Pull day-by-day sales data for comp events from 75_Event_Daily.
    Calculates days_before_event from Report_Date vs parsed EventDate.
    Cost: ~$0.015 per event — safe for 20+ comps.
    """
    if not comp_event_ids:
        return pd.DataFrame()

    ids_str = ", ".join(str(i) for i in comp_event_ids)

    query = f"""
    SELECT
        PID                     AS EventID,
        Name,
        Date                    AS EventDate,
        Venue,
        City,
        State,
        Category,
        Report_Date,
        Revenue,
        Orders,
        Quantity,
        Avg_Order_Size,
        -- Days before event
        DATE_DIFF(
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
            Report_Date,
            DAY
        )                       AS days_before_event
    FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
    WHERE PID IN ({ids_str})
      AND Orders > 0
      AND Avg_Order_Size > 0
      AND Avg_Order_Size < 50000
      AND NOT (
          LOWER(Name) LIKE '%festival%'
          OR LOWER(Name) LIKE '%outlaw%'
          OR LOWER(Name) LIKE '%when we were young%'
          OR LOWER(Name) LIKE '%lollapalooza%'
          OR LOWER(Name) LIKE '%coachella%'
          OR LOWER(Name) LIKE '%bonnaroo%'
      )
    ORDER BY PID, Report_Date ASC
    """

    job = client.query(query)
    df  = job.to_dataframe()
    mb  = job.total_bytes_processed / 1024**2

    print(f"  Rows: {len(df):,} | MB: {mb:.1f} | "
          f"Events: {df['EventID'].nunique() if not df.empty else 0}")

    if df.empty:
        return df

    # Keep only pre-event data points
    df = df[
        (df['days_before_event'] >= 0) &
        (df['days_before_event'] <= 730)
    ].copy()

    return df


# ================================================================== #
#  STEP 2: NORMALIZE PRICE INDEX PER EVENT                            #
# ================================================================== #
def normalize_trajectories(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize Avg_Order_Size to price_index per event.
    price_index = Avg_Order_Size / baseline_price
    baseline_price = median Avg_Order_Size at 60-120 days before event
    (stable window, avoids early noise and late spikes)

    Also calculate cumulative sell-through and velocity metrics.
    """
    if df.empty:
        return df

    results = []
    for event_id, group in df.groupby('EventID'):
        g = group.copy().sort_values('days_before_event', ascending=False)

        # Baseline: median price in 60-120 day window
        baseline_window = g[
            (g['days_before_event'] >= 60) &
            (g['days_before_event'] <= 120)
        ]['Avg_Order_Size']

        if baseline_window.empty:
            # Fallback: use earliest 3 data points
            baseline = g['Avg_Order_Size'].iloc[:3].median()
        else:
            baseline = baseline_window.median()

        if baseline <= 0:
            continue

        g['baseline_price']  = baseline
        g['price_index']     = g['Avg_Order_Size'] / baseline

        # Cumulative orders and revenue
        g = g.sort_values('days_before_event', ascending=False)
        g['cumulative_orders']  = g['Orders'].cumsum()
        g['cumulative_revenue'] = g['Revenue'].cumsum()

        # Rolling 7-day order velocity
        g = g.sort_values('days_before_event', ascending=True)
        g['order_velocity_7d'] = (
            g['Orders'].rolling(window=3, min_periods=1).mean()
        )

        results.append(g)

    if not results:
        return pd.DataFrame()

    return pd.concat(results, ignore_index=True)


# ================================================================== #
#  STEP 3: BUILD AGGREGATE CURVES ACROSS COMPS                        #
# ================================================================== #
def build_aggregate_curves(df: pd.DataFrame,
                           bucket_size: int = 7) -> pd.DataFrame:
    """
    Aggregate normalized trajectories into weekly buckets.
    Produces the "expected trajectory" across all comp events.
    """
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df['day_bucket'] = (df['days_before_event'] // bucket_size) * bucket_size

    curves = (
        df.groupby('day_bucket')
          .agg(
              n_events           = ('EventID',     'nunique'),
              median_price_index = ('price_index', 'median'),
              p25_price_index    = ('price_index', lambda x: x.quantile(0.25)),
              p75_price_index    = ('price_index', lambda x: x.quantile(0.75)),
              median_price       = ('Avg_Order_Size', 'median'),
              median_orders      = ('Orders',      'median'),
              total_orders       = ('Orders',      'sum'),
          )
          .reset_index()
          .sort_values('day_bucket', ascending=False)
    )

    # Only keep buckets with enough events for reliability
    curves = curves[curves['n_events'] >= 2].copy()

    return curves


# ================================================================== #
#  STEP 4: DETECT SELL WINDOWS                                        #
# ================================================================== #
def detect_sell_windows(curves: pd.DataFrame) -> dict:
    """
    Identify key sell windows from the aggregate trajectory:
    - Peak price window (highest median_price_index)
    - High velocity window (most orders per day)
    - Safe early window (stable price, low risk)
    - Last call window (final 14 days)
    """
    if curves.empty:
        return {}

    c = curves.sort_values('day_bucket', ascending=False)

    # Peak price: highest median price index
    peak_idx   = c['median_price_index'].idxmax()
    peak_row   = c.loc[peak_idx]

    # High velocity: most orders
    vel_idx    = c['median_orders'].idxmax()
    vel_row    = c.loc[vel_idx]

    # Safe early: first window where price_index > 0.85
    # and n_events >= 3 (enough data confidence)
    early      = c[
        (c['day_bucket'] >= 60) &
        (c['median_price_index'] >= 0.85) &
        (c['n_events'] >= 3)
    ]
    early_row  = early.iloc[-1] if not early.empty else None

    windows = {
        'peak_price': {
            'days_before': int(peak_row['day_bucket']),
            'price_index': round(float(peak_row['median_price_index']), 3),
            'n_events':    int(peak_row['n_events']),
        },
        'high_velocity': {
            'days_before': int(vel_row['day_bucket']),
            'price_index': round(float(vel_row['median_price_index']), 3),
            'median_orders': round(float(vel_row['median_orders']), 1),
        },
        'safe_early': {
            'days_before': int(early_row['day_bucket']) if early_row is not None else None,
            'price_index': round(float(early_row['median_price_index']), 3) if early_row is not None else None,
        },
        'last_call': {
            'days_before': 14,
            'price_index': round(float(
                c[c['day_bucket'] <= 14]['median_price_index'].median()
            ), 3) if not c[c['day_bucket'] <= 14].empty else None,
        }
    }

    return windows


# ================================================================== #
#  MAIN: BUILD FULL DATASET FOR TARGET EVENT                          #
# ================================================================== #
def build_dataset(target_event_id: int,
                  n_comps: int = 20,
                  save: bool = True) -> dict:

    print(f"\n{'='*65}")
    print(f"BUILDING TRAJECTORY DATASET")
    print(f"Target EventID: {target_event_id}")
    print(f"{'='*65}")

    # ── 1. Profile + comps ──────────────────────────────────────── #
    target = get_event_profile(target_event_id)
    if target is None:
        print("❌ Target event not found.")
        return {}

    print(f"Target: {target['Name']}")
    print(f"Category: {target['Category_Name']}\n")

    _, comps, confidence = find_comps(target_event_id, n_comps=n_comps)
    if comps is None or comps.empty:
        print("❌ No comps found.")
        return {}

    comp_ids = comps['EventID'].tolist()
    print(f"\nConfidence: {confidence} | Comps: {len(comp_ids)}")

    # ── 2. Pull daily trajectories ───────────────────────────────── #
    print(f"\nPulling 75_Event_Daily trajectories...")
    raw_df = pull_daily_trajectories(comp_ids)

    if raw_df.empty:
        print("❌ No trajectory data.")
        return {}

    # ── 3. Normalize ─────────────────────────────────────────────── #
    print("Normalizing price index...")
    norm_df = normalize_trajectories(raw_df)

    # ── 4. Aggregate curves ──────────────────────────────────────── #
    print("Building aggregate curves...")
    curves = build_aggregate_curves(norm_df)

    # ── 5. Detect sell windows ───────────────────────────────────── #
    windows = detect_sell_windows(curves)

    # ── 6. Save ──────────────────────────────────────────────────── #
    if save:
        os.makedirs("data/trajectories", exist_ok=True)
        prefix = f"data/trajectories/{target_event_id}"
        raw_df.to_parquet(f"{prefix}_raw.parquet",    index=False)
        norm_df.to_parquet(f"{prefix}_norm.parquet",  index=False)
        curves.to_parquet(f"{prefix}_curves.parquet", index=False)
        comps.to_csv(f"{prefix}_comps.csv",           index=False)
        print(f"Saved to data/trajectories/{target_event_id}_*")

    # ── 7. Print summary ─────────────────────────────────────────── #
    print(f"\n{'='*65}")
    print(f"RESULTS: {target['Name']}")
    print(f"{'='*65}")
    print(f"Confidence:      {confidence}")
    print(f"Comp events:     {norm_df['EventID'].nunique()}")
    print(f"Data points:     {len(raw_df):,}")
    print(f"Days coverage:   {raw_df['days_before_event'].max()} "
          f"→ {raw_df['days_before_event'].min()} days before event")

    print(f"\nAGGREGATE PRICE TRAJECTORY (weekly buckets):")
    print(curves[[
        'day_bucket', 'n_events',
        'median_price_index', 'p25_price_index', 'p75_price_index',
        'median_price', 'median_orders'
    ]].to_string(index=False))

    print(f"\nSELL WINDOWS:")
    for window, data in windows.items():
        print(f"  {window:<20}: {data}")

    return {
        'target':     target,
        'comps':      comps,
        'confidence': confidence,
        'raw':        raw_df,
        'normalized': norm_df,
        'curves':     curves,
        'windows':    windows,
    }


# ================================================================== #
#  RUN                                                                 #
# ================================================================== #
if __name__ == "__main__":

    test_events = {
        5687566: "NCAA Final Four Championship",
        6487949: "NFL Ravens vs Chargers",
        5045286: "US Open Golf Friday",
        6338949: "Zach Bryan",
        6062329: "My Chemical Romance",
    }

    summaries = {}
    for event_id, label in test_events.items():
        print(f"\n{'#'*65}")
        print(f"# {label}")
        print(f"{'#'*65}")
        try:
            result = build_dataset(event_id, n_comps=15, save=True)
            summaries[event_id] = result
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*65}")
    print("FINAL SUMMARY")
    print(f"{'='*65}")
    for eid, s in summaries.items():
        if s:
            print(f"{s['target']['Name'][:40]:<40} "
                  f"| {s['confidence']:<8} "
                  f"| {s['normalized']['EventID'].nunique()} comps "
                  f"| {len(s['raw']):,} pts")