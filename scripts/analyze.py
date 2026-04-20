#!/usr/bin/env python3
"""
TicketCity Ticket Pricing Analyzer
===================================
Enter an EventID. Get a sell recommendation.
"""

import os
import sys
import importlib.util
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from google.cloud import bigquery

import io
import contextlib

@contextlib.contextmanager
def suppress_output():
    """Suppress stdout from noisy internal functions."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield

# ================================================================== #
#  SETUP                                                               #
# ================================================================== #
client = bigquery.Client(project="ticketcity-tcg")
TODAY  = datetime.now(timezone.utc).strftime('%Y-%m-%d')
TRAJECTORIES_DIR = Path("data/trajectories")
TRAJECTORIES_DIR.mkdir(parents=True, exist_ok=True)

# Import comp engine
spec = importlib.util.spec_from_file_location(
    "comp_engine",
    os.path.join(os.path.dirname(__file__), "04_comp.py")
)
comp_engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comp_engine)

find_comps       = comp_engine.find_comps
get_event_info   = comp_engine.get_event_info

# ================================================================== #
#  STEP 1: GET EVENT INFO                                              #
# ================================================================== #
def fetch_event(event_id: int) -> dict:
    info = get_event_info(event_id)
    if info is None:
        print(f"❌ EventID {event_id} not found in VividIntake.")
        sys.exit(1)
    return info


# ================================================================== #
#  STEP 2: GET CURRENT MARKET PRICE FROM 75_Event_Daily               #
# ================================================================== #
def get_current_market_price(event_id: int) -> dict:
    """
    Pull the most recent sales data for this event from 75_Event_Daily.
    Returns current avg_order_size, orders, and days_before_event.
    """
    query = f"""
    SELECT
        PID,
        Name,
        Date                AS EventDate,
        Report_Date,
        Orders,
        Avg_Order_Size,
        Revenue,
        Quantity,
        DATE_DIFF(
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
            Report_Date,
            DAY
        )                   AS days_before_event
    FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
    WHERE PID = {event_id}
      AND Orders > 0
      AND Avg_Order_Size > 0
    ORDER BY Report_Date DESC
    LIMIT 5
    """
    df = client.query(query).to_dataframe()
    if df.empty:
        return None

    latest = df.iloc[0]
    return {
        'avg_order_size':   float(latest['Avg_Order_Size']),
        'orders':           int(latest['Orders']),
        'days_before_event': int(latest['days_before_event']) if pd.notna(latest['days_before_event']) else None,
        'report_date':      str(latest['Report_Date']),
        'recent_history':   df,
    }


# ================================================================== #
#  STEP 3: BUILD TRAJECTORY FROM COMPS                                 #
# ================================================================== #
def pull_daily_trajectories(comp_event_ids: list) -> pd.DataFrame:
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

    if df.empty:
        return pd.DataFrame()

    df = df[
        (df['days_before_event'] >= 0) &
        (df['days_before_event'] <= 730)
    ].copy()

    print(f"  Trajectory data: {len(df):,} rows | "
          f"{df['EventID'].nunique()} events | {mb:.1f} MB")
    return df


def normalize_trajectories(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    results = []
    for event_id, group in df.groupby('EventID'):
        g = group.copy().sort_values('days_before_event', ascending=False)

        baseline_window = g[
            (g['days_before_event'] >= 60) &
            (g['days_before_event'] <= 120)
        ]['Avg_Order_Size']

        baseline = (baseline_window.median()
                    if not baseline_window.empty
                    else g['Avg_Order_Size'].iloc[:3].median())

        if baseline <= 0:
            continue

        g['baseline_price'] = baseline
        g['price_index']    = g['Avg_Order_Size'] / baseline
        results.append(g)

    if not results:
        return pd.DataFrame()
    return pd.concat(results, ignore_index=True)


def build_aggregate_curves(df: pd.DataFrame,
                           bucket_size: int = 7) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df['day_bucket'] = (df['days_before_event'] // bucket_size) * bucket_size

    curves = (
        df.groupby('day_bucket')
          .agg(
              n_events           = ('EventID',        'nunique'),
              median_price_index = ('price_index',    'median'),
              p25_price_index    = ('price_index',    lambda x: x.quantile(0.25)),
              p75_price_index    = ('price_index',    lambda x: x.quantile(0.75)),
              median_price       = ('Avg_Order_Size', 'median'),
              median_orders      = ('Orders',         'median'),
          )
          .reset_index()
          .sort_values('day_bucket', ascending=False)
    )

    return curves[curves['n_events'] >= 2].copy()


# ================================================================== #
#  STEP 4: PREDICT & RECOMMEND                                         #
# ================================================================== #
def fit_trajectory(curves: pd.DataFrame) -> pd.DataFrame:
    reliable = curves[curves['n_events'] >= 2].sort_values('day_bucket')
    if len(reliable) < 3:
        return pd.DataFrame()

    day_range = range(0, int(reliable['day_bucket'].max()) + 1)
    interp = pd.DataFrame({'day': day_range})

    for col, src in [
        ('price_index',  'median_price_index'),
        ('p25_index',    'p25_price_index'),
        ('p75_index',    'p75_price_index'),
    ]:
        interp[col] = np.interp(
            interp['day'],
            reliable['day_bucket'],
            reliable[src]
        )

    interp['n_events'] = np.interp(
        interp['day'],
        reliable['day_bucket'],
        reliable['n_events']
    ).astype(int)

    interp['price_index_smooth'] = (
        interp['price_index']
          .rolling(window=7, center=True, min_periods=1)
          .median()
    )

    return interp.sort_values('day', ascending=False).reset_index(drop=True)


def get_nearest_row(traj: pd.DataFrame, day: int) -> pd.Series:
    traj = traj.copy()
    traj['dist'] = abs(traj['day'] - day)
    return traj.nsmallest(1, 'dist').iloc[0]


def generate_recommendation(
    event_id: int,
    curves: pd.DataFrame,
    current_price: float,
    days_before_event: int,
    cost_basis: float = None,
) -> dict:

    traj = fit_trajectory(curves)
    if traj.empty:
        return {
            'recommendation': 'INSUFFICIENT_DATA',
            'reasoning': 'Not enough historical comp data to make a prediction.'
        }

    # Current index
    current_row   = get_nearest_row(traj, days_before_event)
    current_index = float(current_row['price_index_smooth'])
    baseline      = current_price / current_index if current_index > 0 \
                    else current_price

    # Future trajectory only
    future = traj[traj['day'] < days_before_event].copy()
    future['abs_price'] = future['price_index_smooth'] * baseline

    if future.empty:
        peak_price, peak_day = current_price, days_before_event
        trough_price = current_price
    else:
        peak_row     = future.loc[future['abs_price'].idxmax()]
        peak_price   = float(peak_row['abs_price'])
        peak_day     = int(peak_row['day'])
        trough_row   = future.loc[future['abs_price'].idxmin()]
        trough_price = float(trough_row['abs_price'])

    day0_row    = get_nearest_row(traj, 0)
    day0_price  = float(day0_row['price_index_smooth']) * baseline

    upside_pct   = ((peak_price   - current_price) / current_price) * 100
    downside_pct = ((trough_price - current_price) / current_price) * 100
    day0_pct     = ((day0_price   - current_price) / current_price) * 100

    # Confidence band width
    avg_band      = float((curves['p75_price_index'] -
                            curves['p25_price_index']).mean())
    uncertainty   = (" ⚠️ High price variability — treat as directional only."
                     if avg_band > 0.8 else "")
    max_support   = int(curves['n_events'].max())
    support_note  = f"Supported by {max_support} historical comp events."

    # Price predictions at key windows
    predictions = {}
    for target_day in [90, 60, 30, 14, 7, 0]:
        if target_day >= days_before_event:
            continue
        row = get_nearest_row(traj, target_day)
        pred_index = float(row['price_index_smooth'])
        pred_price = baseline * pred_index
        change_pct = ((pred_price - current_price) / current_price) * 100
        predictions[target_day] = {
            'predicted_price': round(pred_price, 2),
            'low_estimate':    round(baseline * float(row['p25_index']), 2),
            'high_estimate':   round(baseline * float(row['p75_index']), 2),
            'change_vs_now':   round(change_pct, 1),
            'n_events':        int(row['n_events']),
        }

    # P&L
    pnl = {}
    if cost_basis:
        pnl = {
            'current_profit_pct': round(
                ((current_price - cost_basis) / cost_basis) * 100, 1),
            'peak_profit_pct': round(
                ((peak_price - cost_basis) / cost_basis) * 100, 1),
        }

    # Decision
    risk_reward   = abs(downside_pct) / max(upside_pct, 0.1)
    poor_rr       = risk_reward > 2.5 and downside_pct < -15
    hold_days     = days_before_event - peak_day
    timing_note   = (f" Note: Peak window is only {hold_days} days away"
                     f" — act quickly." if hold_days <= 21 else "")

    if poor_rr and upside_pct < 20:
        rec = 'SELL_NOW'
        reasoning = (
            f"Unfavorable risk/reward — upside of "
            f"+{max(upside_pct,0):.1f}% does not justify downside of "
            f"{downside_pct:.1f}%. Comps show price decay toward "
            f"${day0_price:.0f} at event day. "
            f"{support_note}{uncertainty}"
        )
    elif upside_pct >= 15 and peak_day > 7:
        rec = 'HOLD'
        reasoning = (
            f"Price trajectory shows appreciation to ${peak_price:.0f} "
            f"(+{upside_pct:.1f}%) around {peak_day} days before event. "
            f"{support_note}{uncertainty}{timing_note}"
        )
    elif upside_pct >= 5 and peak_day > 14:
        rec = 'HOLD'
        reasoning = (
            f"Modest upside to ${peak_price:.0f} (+{upside_pct:.1f}%) "
            f"at {peak_day} days out. Downside {downside_pct:.1f}%. "
            f"{support_note}{uncertainty}{timing_note}"
        )
    elif downside_pct < -15 and upside_pct < 5:
        rec = 'SELL_NOW'
        reasoning = (
            f"Comps show consistent decay — expected range "
            f"${trough_price:.0f}–${peak_price:.0f}, day-of ~${day0_price:.0f} "
            f"({day0_pct:.1f}% vs now). {support_note}{uncertainty}"
        )
    elif abs(upside_pct) < 5 and abs(downside_pct) < 10:
        rec = 'MONITOR'
        reasoning = (
            f"Flat trajectory. Upside {upside_pct:.1f}%, "
            f"downside {downside_pct:.1f}%. No urgency. "
            f"Re-evaluate in 14 days. {support_note}{uncertainty}"
        )
    elif day0_pct < -10 and upside_pct < 10:
        rec = 'SELL_NOW'
        reasoning = (
            f"Comps show price decays to ~${day0_price:.0f} by event day "
            f"({day0_pct:.1f}%). Limited upside doesn't justify the risk. "
            f"{support_note}{uncertainty}"
        )
    else:
        rec = 'MONITOR'
        reasoning = (
            f"Mixed signals — upside {upside_pct:.1f}%, "
            f"downside {downside_pct:.1f}%. "
            f"Review in 21 days. {support_note}{uncertainty}"
        )

    return {
        'recommendation':   rec,
        'reasoning':        reasoning,
        'current_price':    round(current_price, 2),
        'baseline_price':   round(baseline, 2),
        'days_remaining':   days_before_event,
        'peak_price':       round(peak_price, 2),
        'peak_day':         peak_day,
        'trough_price':     round(trough_price, 2),
        'day0_price':       round(day0_price, 2),
        'upside_pct':       round(upside_pct, 1),
        'downside_pct':     round(downside_pct, 1),
        'day0_pct':         round(day0_pct, 1),
        'predictions':      predictions,
        **pnl,
    }


# ================================================================== #
#  STEP 5: PRINT FORMATTED REPORT                                      #
# ================================================================== #
def print_report(info: dict, comps: pd.DataFrame,
                 confidence: str, market: dict,
                 rec: dict):

    print(f"\n{'='*65}")
    print(f"  TICKETCITY ANALYSIS REPORT")
    print(f"{'='*65}")
    print(f"  Event:    {info['Name']}")
    print(f"  Date:     {info['LocalDate']}")
    print(f"  Venue:    {info['Venue_Name']}, {info['City']}, {info['State']}")
    print(f"  Category: {info['Category_Name']}")
    print(f"{'='*65}")

    print(f"\n📊 MARKET SNAPSHOT")
    print(f"  Current listing range:  ${info['MinPrice']} – ${info['MaxPrice']}")
    print(f"  Active listings:        {info['ListingCount']:,}")
    if market:
        print(f"  Most recent sale price: ${market['avg_order_size']:.2f} "
              f"({market['days_before_event']} days before event, "
              f"{market['report_date']})")
        print(f"  Orders on that date:    {market['orders']}")

    print(f"\n🔍 COMP ANALYSIS")
    print(f"  Confidence:    {confidence}")
    print(f"  Comps used:    {len(comps)}")
    if not comps.empty:
        print(f"  Top 3 comps:")
        for _, row in comps.head(3).iterrows():
            print(f"    • {row['Name'][:50]:<50} "
                  f"| {row['EventDate'][:10]} "
                  f"| ${row['avg_order_size']:.0f} avg "
                  f"| {row['total_orders']:.0f} orders")

    print(f"\n📈 PRICE PREDICTIONS")
    print(f"  Baseline price: ${rec['baseline_price']}")
    for day, data in sorted(rec.get('predictions', {}).items(), reverse=True):
        direction = "↑" if data['change_vs_now'] > 0 else "↓"
        print(f"  At {day:>3} days:  "
              f"${data['predicted_price']:>7.2f}  "
              f"{direction}{abs(data['change_vs_now']):.1f}%  "
              f"[${data['low_estimate']:.0f}–${data['high_estimate']:.0f}]  "
              f"({data['n_events']} events)")

    # Recommendation box
    icons = {'SELL_NOW': '🔴', 'HOLD': '🟢', 'MONITOR': '🟡',
             'INSUFFICIENT_DATA': '⚪'}
    icon  = icons.get(rec['recommendation'], '⚪')

    print(f"\n{icon} RECOMMENDATION: {rec['recommendation']}")
    print(f"  {rec['reasoning']}")

    if 'current_profit_pct' in rec:
        print(f"\n💰 P&L SUMMARY")
        print(f"  Current P&L:  {rec['current_profit_pct']:+.1f}%")
        print(f"  Peak P&L:     {rec['peak_profit_pct']:+.1f}%")

    peak_label = ("(no future upside — at or past peak)"
                  if rec['peak_price'] <= rec['current_price'] * 1.02
                  else f"target: sell at {rec['peak_day']} days before event")
    print(f"\n  Peak window:  ${rec['peak_price']:.2f} — {peak_label}")
    print(f"  Day-of price: ${rec['day0_price']:.2f} "
          f"({rec['day0_pct']:+.1f}% vs now)")
    print(f"{'='*65}\n")


# ================================================================== #
#  MAIN PIPELINE                                                       #
# ================================================================== #
def run_pipeline(event_id: int,
                 current_price: float = None,
                 days_before: int = None,
                 cost_basis: float = None,
                 n_comps: int = 15) -> dict:

    print(f"\n⏳ Fetching event info...")
    info = fetch_event(event_id)

    print(f"   {info['Name']}")
    print(f"   {info['Category_Name']} | "
          f"{info['Venue_Name']}, {info['City']}")

    print(f"\n⏳ Fetching current market data...")
    market = get_current_market_price(event_id)

    # Auto-detect current price and days if not provided
    if current_price is None:
        if market and market['avg_order_size']:
            current_price = market['avg_order_size']
            print(f"   Auto-detected price: ${current_price:.2f} "
                  f"(most recent sale)")
        else:
            current_price = float(info['MinPrice'])
            print(f"   Using MinPrice as proxy: ${current_price:.2f}")

    if days_before is None:
        if market and market['days_before_event']:
            days_before = market['days_before_event']
            print(f"   Auto-detected days before event: {days_before}")
        else:
            # Calculate from LocalDate
            try:
                event_dt = datetime.fromisoformat(
                    str(info['LocalDate']).replace(' ', 'T')
                )
                days_before = max(0, (event_dt - datetime.now()).days)
                print(f"   Calculated days before event: {days_before}")
            except Exception:
                days_before = 90
                print(f"   Defaulting to 90 days before event")

    print(f"\n⏳ Finding comparable events...")
    _, comps, confidence = find_comps(event_id, n_comps=n_comps)

    if comps is None or comps.empty:
        print("❌ No comps found. Cannot generate recommendation.")
        return {}

    # Check cache — skip trajectory pull if already done today
    cache_path = TRAJECTORIES_DIR / f"{event_id}_curves.parquet"
    if cache_path.exists():
        mod_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
        age_hours = (datetime.now() - mod_time).total_seconds() / 3600
        if age_hours < 24:
            print(f"\n⏳ Loading cached trajectories "
                  f"(built {age_hours:.0f}h ago)...")
            curves = pd.read_parquet(cache_path)
        else:
            curves = None
    else:
        curves = None

    if curves is None:
        print(f"\n⏳ Building price trajectories...")
        comp_ids = comps['EventID'].tolist()
        raw_df   = pull_daily_trajectories(comp_ids)

        if raw_df.empty:
            print("❌ No trajectory data found.")
            return {}

        norm_df = normalize_trajectories(raw_df)
        curves  = build_aggregate_curves(norm_df)

        # Cache results
        curves.to_parquet(cache_path, index=False)
        norm_df.to_parquet(
            TRAJECTORIES_DIR / f"{event_id}_norm.parquet", index=False
        )
        comps.to_csv(
            TRAJECTORIES_DIR / f"{event_id}_comps.csv", index=False
        )
        print(f"   Trajectory cached to data/trajectories/")

    print(f"\n⏳ Generating recommendation...")
    rec = generate_recommendation(
        event_id        = event_id,
        curves          = curves,
        current_price   = current_price,
        days_before_event = days_before,
        cost_basis      = cost_basis,
    )

    print_report(info, comps, confidence, market, rec)

    return {
        'event_id':   event_id,
        'info':       info,
        'comps':      comps,
        'confidence': confidence,
        'market':     market,
        'curves':     curves,
        'rec':        rec,
    }


# ================================================================== #
#  ENTRY POINT                                                         #
# ================================================================== #
if __name__ == "__main__":

    print("\n" + "="*65)
    print("  TICKETCITY PRICING ANALYZER")
    print("="*65)

    # Get EventID
    event_input = input("\nEnter EventID: ").strip()
    if not event_input.isdigit():
        print("❌ Invalid EventID.")
        sys.exit(1)
    event_id = int(event_input)

    # Optional: cost basis
    cost_input = input(
        "Cost basis per ticket (press Enter to skip): "
    ).strip()
    cost_basis = float(cost_input) if cost_input else None

    # Run everything
    run_pipeline(
        event_id   = event_id,
        cost_basis = cost_basis,
    )