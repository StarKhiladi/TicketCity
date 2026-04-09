#!/usr/bin/env python3
"""
TicketCity Pricing Model Backtester
=====================================
Properly time-isolated backtesting across diverse events.
No data leakage — comps only use data available BEFORE the snapshot day.
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from google.cloud import bigquery
import importlib.util
import warnings
warnings.filterwarnings('ignore')

# ================================================================== #
#  SETUP                                                               #
# ================================================================== #
client = bigquery.Client(project="ticketcity-tcg")
TODAY  = datetime.now(timezone.utc).strftime('%Y-%m-%d')
BACKTEST_DIR = Path("data/backtest")
BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

# Import main pipeline
spec = importlib.util.spec_from_file_location(
    "main",
    os.path.join(os.path.dirname(__file__), "main.py")
)
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

def safe_sql_str(s: str) -> str:
    """Sanitize string for SQL interpolation."""
    return str(s).replace("'", "''").replace("\\", "\\\\")


# ================================================================== #
#  STEP 1: FIND DIVERSE BACKTEST CANDIDATES                           #
# ================================================================== #
def find_diverse_candidates(
    category: str,
    n_events: int = 15,
    start_year: int = 2022,
    end_year: int = 2025,
) -> pd.DataFrame:
    """
    Find completed events with good trajectory coverage.
    Stratified by team/performer and year to avoid bias.
    Only events that ENDED before today (completed events).
    Only events with data from 60+ days before through day 0.
    """
    query = f"""
    WITH event_stats AS (
        SELECT
            PID,
            MAX(Name)                       AS Name,
            MAX(Date)                       AS EventDate,
            MAX(Venue)                      AS Venue,
            MAX(City)                       AS City,
            MAX(Category)                   AS Category,
            EXTRACT(YEAR FROM MIN(Report_Date)) AS data_start_year,
            COUNT(DISTINCT Report_Date)     AS n_data_points,
            SUM(Orders)                     AS total_orders,
            AVG(Avg_Order_Size)             AS overall_avg_price,
            MAX(
                DATE_DIFF(
                    SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
                    Report_Date, DAY
                )
            )                               AS max_days_before,
            MIN(
                DATE_DIFF(
                    SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
                    Report_Date, DAY
                )
            )                               AS min_days_before,
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(MAX(Date), 1, 8))
                                            AS event_date_parsed
        FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
        WHERE Category = '{category}'
          AND Orders > 0
          AND Avg_Order_Size > 0
          AND Avg_Order_Size < 50000
          AND Name NOT LIKE '%Test%'
          AND Name NOT LIKE '%arking%'
          AND Name NOT LIKE '%Gift Card%'
          AND Name NOT LIKE '%distanc%'
          AND Name NOT LIKE '%Reduced Capacity%'
          AND Name NOT LIKE '%Limited Capacity%'
          AND Name NOT LIKE '%Preseason%'
          AND Name NOT LIKE '%Pre-Season%'
          AND Name NOT LIKE '%Spring Training%'
          AND Name NOT LIKE '%Exhibition%'
        GROUP BY PID
        HAVING
            -- Must have completed (event date in past)
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(MAX(Date), 1, 8))
                < CURRENT_DATE()
            -- Event must be in our target year range
            AND EXTRACT(YEAR FROM
                SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(MAX(Date), 1, 8))
            ) BETWEEN {start_year} AND {end_year}
            -- Must have data from at least 60 days before
            AND MAX(
                DATE_DIFF(
                    SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
                    Report_Date, DAY
                )
            ) >= 60
            -- Must have data close to event day
            AND MIN(
                DATE_DIFF(
                    SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
                    Report_Date, DAY
                )
            ) <= 7
            -- Enough orders to be meaningful
            AND SUM(Orders) >= 50
            AND COUNT(DISTINCT Report_Date) >= 5
    ),
    -- Deduplicate similar events (same venue+date = same event)
    ranked AS (
        SELECT *,
            ROW_NUMBER() OVER (
                -- Stratify by city and year to get diversity
                PARTITION BY City, EXTRACT(YEAR FROM event_date_parsed)
                ORDER BY total_orders DESC
            ) AS city_year_rank
        FROM event_stats
    )
    SELECT *
    FROM ranked
    WHERE city_year_rank <= 2  -- max 2 events per city per year
    ORDER BY RAND()             -- random selection for diversity
    LIMIT {n_events}
    """

    df = client.query(query).to_dataframe()
    print(f"  Found {len(df)} diverse candidates for {category}")
    if not df.empty:
        print(f"  Year range: {df['data_start_year'].min():.0f}"
              f" – {df['data_start_year'].max():.0f}")
        print(f"  Cities: {df['City'].nunique()} unique")
    return df


# ================================================================== #
#  STEP 2: TIME-ISOLATED COMP FINDING                                  #
# ================================================================== #
def find_comps_before_date(
    event_id: int,
    event_name: str,
    event_date_str: str,
    category: str,
    snapshot_day: int,
    avg_price: float,
    city: str,
    venue: str,
    n_comps: int = 10,
) -> pd.DataFrame:
    """
    Find comp events using ONLY data that would have been available
    at snapshot_day days before the target event.

    snapshot_date = event_date - snapshot_day days
    Comps must have ENDED before snapshot_date.
    This prevents data leakage from future events.
    """
    # Parse event date
    event_dt = None
    for fmt in ['%m/%d/%y %I:%M %p', '%m/%d/%Y %I:%M %p',
                '%Y-%m-%d %H:%M:%S', '%m/%d/%y']:
        try:
            event_dt = datetime.strptime(
                str(event_date_str).strip()[:17], fmt
            )
            break
        except ValueError:
            continue

    if event_dt is None:
        return pd.DataFrame()

    # The "knowledge cutoff" — what we knew at snapshot day
    from datetime import timedelta
    snapshot_date = event_dt - timedelta(days=snapshot_day)
    snapshot_date_str = snapshot_date.strftime('%Y-%m-%d')

    # Find completed comps that existed before our snapshot date
    query = f"""
    WITH comp_stats AS (
        SELECT
            PID,
            MAX(Name)                       AS Name,
            MAX(Date)                       AS EventDate,
            MAX(Venue)                      AS Venue,
            MAX(City)                       AS City,
            SUM(Orders)                     AS total_orders,
            AVG(Avg_Order_Size)             AS avg_order_size,
            SUM(Quantity)                   AS total_quantity,
            MIN(Report_Date)                AS first_report,
            MAX(Report_Date)                AS last_report,
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(MAX(Date), 1, 8))
                                            AS event_date_parsed
        FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
        WHERE Category = '{category}'
          AND PID != {event_id}
          AND Orders > 0
          AND Avg_Order_Size > 0
          AND Name NOT LIKE '%Test%'
          AND Name NOT LIKE '%arking%'
          AND Name NOT LIKE '%distanc%'
          AND Name NOT LIKE '%Reduced Capacity%'
          AND Name NOT LIKE '%arking%'
          AND Name NOT LIKE '%Gift Card%'
          AND Name NOT LIKE '%All Session%'
          AND Name NOT LIKE '%Season Pass%'
          AND Name NOT LIKE '%Preseason%'
          AND Name NOT LIKE '%Pre-Season%'
          AND Name NOT LIKE '%festival%'
          -- Only use data available at snapshot time
          AND Report_Date <= '{snapshot_date_str}'
        GROUP BY PID
        HAVING
            -- Event must have COMPLETED before our snapshot date
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(MAX(Date), 1, 8))
                < DATE('{snapshot_date_str}')
            AND SUM(Orders) >= 20
    ),
    filtered AS (
        SELECT *
        FROM comp_stats
        WHERE avg_order_size BETWEEN {avg_price * 0.2}
                                 AND {avg_price * 5.0}
    )
    SELECT
        PID                         AS EventID,
        Name,
        EventDate,
        Venue,
        City,
        total_orders,
        total_quantity,
        avg_order_size,
        first_report,
        last_report,
        (
            -- Same city bonus
            CASE WHEN City = '{safe_sql_str(city)}'
                 THEN 3 ELSE 0 END +
            -- Same venue bonus
            CASE WHEN Venue = '{safe_sql_str(venue)}'
                 THEN 4 ELSE 0 END +
            -- Price similarity
            CASE WHEN avg_order_size BETWEEN {avg_price * 0.4}
                                         AND {avg_price * 2.5}
                 THEN 3 ELSE 0 END +
            -- Recency bonus
            CASE WHEN last_report >= '2023-01-01' THEN 2 ELSE 0 END +
            -- Volume signal
            CASE WHEN total_orders > 200 THEN 1 ELSE 0 END
        )                           AS similarity_score
    FROM filtered
    ORDER BY similarity_score DESC, total_orders DESC
    LIMIT {n_comps}
    """

    return client.query(query).to_dataframe()


# ================================================================== #
#  STEP 3: TIME-ISOLATED TRAJECTORY                                    #
# ================================================================== #
def build_comp_trajectory_before_date(
    comp_ids: list,
    snapshot_date_str: str,
) -> pd.DataFrame:
    """
    Build trajectory using ONLY data available before snapshot_date.
    Prevents leakage of future price data into historical predictions.
    """
    if not comp_ids:
        return pd.DataFrame()

    ids_str = ", ".join(str(i) for i in comp_ids)

    query = f"""
    SELECT
        PID                     AS EventID,
        Date                    AS EventDate,
        Report_Date,
        Orders,
        Avg_Order_Size,
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
      -- Only use data available at snapshot time
      AND Report_Date <= '{snapshot_date_str}'
      AND NOT (
          LOWER(Name) LIKE '%festival%'
          OR LOWER(Name) LIKE '%outlaw%'
          OR LOWER(Name) LIKE '%lollapalooza%'
      )
    ORDER BY PID, Report_Date ASC
    """

    df = client.query(query).to_dataframe()
    if df.empty:
        return pd.DataFrame()

    df = df[
        (df['days_before_event'] >= 0) &
        (df['days_before_event'] <= 730)
    ].copy()

    return df


# ================================================================== #
#  STEP 4: GET ACTUAL OUTCOME                                          #
# ================================================================== #
def get_actual_outcome(event_id: int, snapshot_day: int) -> dict:
    """
    Get what actually happened AFTER the snapshot day.
    This is the ground truth we compare against.
    """
    query = f"""
    SELECT
        Report_Date,
        Orders,
        Avg_Order_Size,
        DATE_DIFF(
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
            Report_Date,
            DAY
        )                       AS days_before_event
    FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
    WHERE PID = {event_id}
      AND Orders > 0
      AND Avg_Order_Size > 0
      AND DATE_DIFF(
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
            Report_Date, DAY
          ) <= {snapshot_day}
    ORDER BY Report_Date ASC
    """
    df = client.query(query).to_dataframe()
    if df.empty:
        return None

    day0_rows = df[df['days_before_event'] <= 5]
    if day0_rows.empty:
        return None

    # Find peak price AFTER snapshot day
    peak_row = df.loc[df['Avg_Order_Size'].idxmax()]

    return {
        'day0_price':     float(day0_rows['Avg_Order_Size'].mean()),
        'day0_orders':    int(day0_rows['Orders'].sum()),
        'peak_price':     float(peak_row['Avg_Order_Size']),
        'peak_day':       int(peak_row['days_before_event']),
        'history':        df,
    }


# ================================================================== #
#  STEP 5: EVALUATE ACCURACY                                           #
# ================================================================== #
def evaluate(rec: dict, actual: dict) -> dict:
    if actual is None:
        return {'correct': None, 'outcome': 'NO_DATA'}

    snap_price   = rec['snapshot_price']
    day0_price   = actual['day0_price']
    peak_price   = actual['peak_price']

    actual_day0_chg  = ((day0_price  - snap_price) / snap_price) * 100
    actual_peak_chg  = ((peak_price  - snap_price) / snap_price) * 100

    recommendation = rec['recommendation']

    if recommendation == 'SELL_NOW':
        # Correct if price fell meaningfully or stayed flat
        correct = actual_day0_chg < 5
        outcome = ('CORRECT' if correct
                   else f'WRONG — price rose {actual_day0_chg:.1f}%')

    elif recommendation == 'HOLD':
        # Correct if price genuinely appreciated after snapshot
        correct = actual_peak_chg >= 10
        outcome = ('CORRECT' if correct
                   else f'WRONG — peak only {actual_peak_chg:.1f}%')

    elif recommendation == 'MONITOR':
        # Correct if trajectory was flat
        correct = abs(actual_day0_chg) < 15
        outcome = ('CORRECT' if correct
                   else f'MIXED — actual {actual_day0_chg:.1f}%')
    else:
        correct = None
        outcome = 'INSUFFICIENT_DATA'

    return {
        'correct':           correct,
        'outcome':           outcome,
        'actual_day0_chg':   round(actual_day0_chg, 1),
        'actual_peak_chg':   round(actual_peak_chg, 1),
        'actual_day0_price': round(day0_price, 2),
        'actual_peak_price': round(peak_price, 2),
    }


# ================================================================== #
#  STEP 6: FULL BACKTEST                                               #
# ================================================================== #
def run_backtest(
    categories: list,
    snapshot_days: list = None,
    n_events_per_category: int = 10,
    n_comps: int = 10,
    start_year: int = 2022,
    end_year: int = 2025,
) -> pd.DataFrame:

    if snapshot_days is None:
        snapshot_days = [90, 60, 30, 14]

    # Must be sorted descending — we walk from furthest to closest
    snapshot_days = sorted(snapshot_days, reverse=True)

    from datetime import timedelta
    all_results = []

    for category in categories:
        print(f"\n{'='*60}")
        print(f"BACKTESTING: {category}")
        print(f"{'='*60}")

        candidates = find_diverse_candidates(
            category   = category,
            n_events   = n_events_per_category,
            start_year = start_year,
            end_year   = end_year,
        )

        if candidates.empty:
            print(f"  No candidates found.")
            continue

        for _, row in candidates.iterrows():
            event_id   = int(row['PID'])
            event_name = str(row['Name'])
            event_date = str(row['EventDate'])
            city       = str(row['City'])
            venue      = str(row['Venue'])
            avg_price  = float(row['overall_avg_price'])
            max_days   = int(row['max_days_before'])

            print(f"\n  [{event_id}] {event_name[:55]}")
            print(f"  Date: {event_date} | City: {city} | "
                  f"Avg price: ${avg_price:.0f}")

            # Parse event date
            event_dt = None
            for fmt in ['%m/%d/%y %I:%M %p', '%m/%d/%Y %I:%M %p',
                        '%Y-%m-%d %H:%M:%S', '%m/%d/%y']:
                try:
                    event_dt = datetime.strptime(
                        str(event_date).strip()[:17], fmt
                    )
                    break
                except ValueError:
                    continue

            if event_dt is None:
                print(f"  ❌ Cannot parse date: {event_date}")
                continue

            # Get full price history for this event
            actual_full = get_actual_outcome(event_id, max_days)
            if actual_full is None:
                print(f"  ❌ No price history")
                continue

            history = actual_full['history']

            # Baseline: sell at day 90 (or earliest available snapshot)
            first_snap_day = snapshot_days[0]  # 90
            near_first = history[
                abs(history['days_before_event'] - first_snap_day) <= 14
            ]
            if near_first.empty:
                print(f"  ❌ No price data near day {first_snap_day}")
                continue

            baseline_price    = float(near_first['Avg_Order_Size'].median())
            baseline_sell_day = int(near_first['days_before_event'].median())
            actual_day0_price  = actual_full['day0_price']

            # Baseline profit = sell at day 90
            baseline_profit_pct = (
                (baseline_price - baseline_price) / baseline_price * 100
            )  # 0% — this is our reference point

            print(f"  Baseline (sell at day {baseline_sell_day}): "
                  f"${baseline_price:.0f}")

            # Model simulation: walk through checkpoints
            # Stop at first SELL_NOW
            model_sell_price  = None
            model_sell_day    = None
            model_sell_reason = None
            checkpoints       = []

            for snap_day in snapshot_days:
                if snap_day > max_days - 7:
                    continue

                snapshot_dt  = event_dt - timedelta(days=snap_day)
                snapshot_str = snapshot_dt.strftime('%Y-%m-%d')

                # Find comps available at this snapshot date
                comps = find_comps_before_date(
                    event_id       = event_id,
                    event_name     = event_name,
                    event_date_str = event_date,
                    category       = category,
                    snapshot_day   = snap_day,
                    avg_price      = avg_price,
                    city           = city,
                    venue          = venue,
                    n_comps        = n_comps,
                )

                if comps is None or comps.empty:
                    checkpoints.append({
                        'snap_day': snap_day,
                        'rec': 'NO_COMPS',
                        'price': None,
                    })
                    continue

                comp_ids = comps['EventID'].tolist()

                raw_df = build_comp_trajectory_before_date(
                    comp_ids          = comp_ids,
                    snapshot_date_str = snapshot_str,
                )

                if raw_df.empty:
                    checkpoints.append({
                        'snap_day': snap_day,
                        'rec': 'NO_DATA',
                        'price': None,
                    })
                    continue

                norm_df = main.normalize_trajectories(raw_df)
                curves  = main.build_aggregate_curves(norm_df)

                if curves.empty:
                    checkpoints.append({
                        'snap_day': snap_day,
                        'rec': 'NO_CURVES',
                        'price': None,
                    })
                    continue

                # Get price at this snapshot day
                near_snap = history[
                    abs(history['days_before_event'] - snap_day) <= 14
                ]
                if near_snap.empty:
                    checkpoints.append({
                        'snap_day': snap_day,
                        'rec': 'NO_PRICE',
                        'price': None,
                    })
                    continue

                snapshot_price = float(
                    near_snap['Avg_Order_Size'].median()
                )

                # Get recommendation
                try:
                    rec_result = main.generate_recommendation(
                        curves            = curves,
                        current_price     = snapshot_price,
                        days_before_event = snap_day,
                    )
                except Exception as e:
                    checkpoints.append({
                        'snap_day': snap_day,
                        'rec': f'ERROR',
                        'price': snapshot_price,
                    })
                    continue

                rec = rec_result['recommendation']

                checkpoints.append({
                    'snap_day':      snap_day,
                    'rec':           rec,
                    'price':         snapshot_price,
                    'upside_pct':    rec_result['upside_pct'],
                    'downside_pct':  rec_result['downside_pct'],
                })

                # First SELL_NOW = model says sell here
                # But require day-60 confirmation before selling at day-90
                # (day-90 price is often noisy)
                if rec == 'SELL_NOW' and model_sell_price is None:
                    if snap_day < 90:  # only sell if not at first checkpoint
                        model_sell_price  = snapshot_price
                        model_sell_day    = snap_day
                        model_sell_reason = rec
                    else:
                        # At day 90, downgrade SELL_NOW to MONITOR
                        # Wait for day-60 confirmation
                        checkpoints[-1]['rec'] = 'MONITOR_90'

            # If model never said SELL_NOW, sell at day 0 (event day)
            if model_sell_price is None:
                model_sell_price = actual_day0_price
                model_sell_day   = 0
                model_sell_reason = 'HELD_TO_EVENT'

            # Calculate outcomes
            model_vs_baseline = model_sell_price - baseline_price
            model_vs_day0     = model_sell_price - actual_day0_price
            model_pct_vs_baseline = (
                model_vs_baseline / baseline_price * 100
            )

            # Print checkpoint summary
            checkpoint_str = " → ".join([
                f"Day {c['snap_day']}:{c['rec'][:4]}"
                f"(${c['price']:.0f})" if c['price'] else
                f"Day {c['snap_day']}:{c['rec']}"
                for c in checkpoints
            ])
            print(f"  Path: {checkpoint_str}")
            print(f"  Baseline sell @ day {baseline_sell_day}: "
                  f"${baseline_price:.0f}")
            print(f"  Model sell   @ day {model_sell_day}:  "
                  f"${model_sell_price:.0f}  "
                  f"({model_pct_vs_baseline:+.1f}% vs baseline)  "
                  f"{'✅' if model_vs_baseline > 0 else '❌'}")
            print(f"  Day-0 price: ${actual_day0_price:.0f}")

            all_results.append({
                'category':              category,
                'event_id':              event_id,
                'event_name':            event_name[:50],
                'city':                  city,
                'event_date':            event_date,
                'baseline_price':        round(baseline_price, 2),
                'baseline_sell_day':     baseline_sell_day,
                'model_sell_price':      round(model_sell_price, 2),
                'model_sell_day':        model_sell_day,
                'model_sell_reason':     model_sell_reason,
                'actual_day0_price':     round(actual_day0_price, 2),
                'model_vs_baseline':     round(model_vs_baseline, 2),
                'model_pct_vs_baseline': round(model_pct_vs_baseline, 1),
                'model_beat_baseline':   model_vs_baseline > 0,
                'model_beat_day0':       model_sell_price > actual_day0_price,
                'checkpoints':           str(checkpoints),
            })

    return pd.DataFrame(all_results)


def print_summary(results: pd.DataFrame):
    if results.empty:
        print("No results to summarize.")
        return

    print(f"\n{'='*65}")
    print(f"BACKTEST SUMMARY — MODEL vs SELL IMMEDIATELY AT DAY 90")
    print(f"{'='*65}")
    print(f"Events evaluated:   {len(results)}")
    print(f"Categories:         {results['category'].nunique()}")

    # Core question: does the model beat selling immediately?
    beat_rate = results['model_beat_baseline'].mean() * 100
    avg_improvement = results['model_vs_baseline'].mean()
    median_improvement = results['model_vs_baseline'].median()

    

    # When did the model sell?
    print(f"\nWHEN DID THE MODEL SELL?")
    sell_day_counts = results['model_sell_day'].value_counts().sort_index(ascending=False)
    for day, count in sell_day_counts.items():
        pct = count / len(results) * 100
        avg_vs_baseline = results[
            results['model_sell_day'] == day
        ]['model_vs_baseline'].mean()
        print(f"  Day {day:>3}: {count:>2} events ({pct:.0f}%)  "
              f"avg vs baseline: ${avg_vs_baseline:+.0f}")

    # By category
    if results['category'].nunique() > 1:
        print(f"\nRESULTS BY CATEGORY:")
        for cat in results['category'].unique():
            sub = results[results['category'] == cat]
            beat = sub['model_beat_baseline'].mean() * 100
            avg  = sub['model_vs_baseline'].mean()
            print(f"  {cat:<25}: {beat:.0f}% beat baseline  "
                  f"avg ${avg:+.0f}/ticket")

    # Best and worst outcomes
    print(f"\nBEST MODEL DECISIONS:")
    best = results.nlargest(3, 'model_vs_baseline')
    for _, r in best.iterrows():
        print(f"  {r['event_name'][:40]:<40} "
              f"sold day {r['model_sell_day']:>3} "
              f"${r['model_sell_price']:.0f} vs "
              f"${r['baseline_price']:.0f} baseline "
              f"({r['model_pct_vs_baseline']:+.1f}%)")

    print(f"\nWORST MODEL DECISIONS:")
    worst = results.nsmallest(3, 'model_vs_baseline')
    for _, r in worst.iterrows():
        print(f"  {r['event_name'][:40]:<40} "
              f"sold day {r['model_sell_day']:>3} "
              f"${r['model_sell_price']:.0f} vs "
              f"${r['baseline_price']:.0f} baseline "
              f"({r['model_pct_vs_baseline']:+.1f}%)")

    # Compare model vs just waiting until day 0
    print(f"\nMODEL vs WAITING UNTIL EVENT DAY:")
    model_avg  = results['model_sell_price'].mean()
    day0_avg   = results['actual_day0_price'].mean()
    base_avg   = results['baseline_price'].mean()
    print(f"  Sell at day 90 (baseline): ${base_avg:.0f} avg")
    print(f"  Model recommendation:      ${model_avg:.0f} avg "
          f"({(model_avg-base_avg)/base_avg*100:+.1f}% vs baseline)")
    print(f"  Wait until event day:      ${day0_avg:.0f} avg "
          f"({(day0_avg-base_avg)/base_avg*100:+.1f}% vs baseline)")
    
    beat_day0_rate = results['model_beat_day0'].mean() * 100
    print(f"  Model beats holding to day 0:  {beat_day0_rate:.1f}% of events")

    print(f"\n{'='*65}")


# ================================================================== #
#  ENTRY POINT                                                         #
# ================================================================== #
if __name__ == "__main__":

    print("\n" + "="*65)
    print("  TICKETCITY MODEL BACKTESTER (Time-Isolated)")
    print("="*65)
    print("\nThis backtester uses strict time isolation —")
    print("comps only use data available BEFORE each snapshot date.\n")

    print("Select backtest scope:")
    print("  1. Quick — NFL Football only (~15 min)")
    print("  2. Sports — NFL + NBA + MLB + NHL")
    print("  3. Full — all categories (slowest)")
    print("  4. Custom category")

    choice = input("\nChoice (1-4): ").strip()

    if choice == '1':
        categories = ['NFL Football']
        n_per_cat  = 10
        snap_days  = [90, 60, 30, 14]

    elif choice == '2':
        categories = ['NFL Football', 'NBA Basketball',
                      'MLB Baseball', 'NHL Hockey']
        n_per_cat  = 8
        snap_days  = [90, 60, 30, 14]

    elif choice == '3':
        categories = [
            'NFL Football', 'NBA Basketball', 'MLB Baseball',
            'NHL Hockey', 'NCAA Basketball', 'Rock', 'Pop', 'Comedy'
        ]
        n_per_cat  = 5
        snap_days  = [90, 60, 30, 14]

    elif choice == '4':
        cat_input  = input("Enter category: ").strip()
        categories = [cat_input]
        n_per_cat  = 10
        snap_days  = [90, 60, 30, 14]

    else:
        categories = ['NFL Football']
        n_per_cat  = 5
        snap_days  = [90, 60, 30, 14]

    results = run_backtest(
        categories            = categories,
        snapshot_days         = snap_days,
        n_events_per_category = n_per_cat,
        start_year            = 2022,
        end_year              = 2025,
    )

    if not results.empty:
        print_summary(results)
        out_path = BACKTEST_DIR / f"backtest_{TODAY}.csv"
        results.to_csv(out_path, index=False)
        print(f"\nSaved to {out_path}")
    else:
        print("\n❌ No results generated.")