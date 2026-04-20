import pandas as pd
import numpy as np
from pathlib import Path
import json
import os

# ================================================================== #
#  PRICE PREDICTION MODEL                                              #
#  Uses normalized trajectory curves from Script 06 to:               #
#  1. Predict price at any days_before_event                          #
#  2. Recommend optimal sell window                                    #
#  3. Score hold vs sell decision                                      #
# ================================================================== #

TRAJECTORIES_DIR = Path("data/trajectories")

# ================================================================== #
#  STEP 1: LOAD CURVES                                                 #
# ================================================================== #
def load_curves(event_id: int) -> pd.DataFrame:
    path = TRAJECTORIES_DIR / f"{event_id}_curves.parquet"
    if not path.exists():
        raise FileNotFoundError(f"No curves found for {event_id}. "
                                f"Run 06_trajectory_builder.py first.")
    return pd.read_parquet(path)


def load_normalized(event_id: int) -> pd.DataFrame:
    path = TRAJECTORIES_DIR / f"{event_id}_norm.parquet"
    if not path.exists():
        raise FileNotFoundError(f"No normalized data for {event_id}.")
    return pd.read_parquet(path)


# ================================================================== #
#  STEP 2: FIT SMOOTH TRAJECTORY                                       #
# ================================================================== #
def fit_trajectory(curves: pd.DataFrame,
                   min_events: int = 2) -> pd.DataFrame:
    """
    Fit a smoothed price index trajectory using rolling median.
    Filters to buckets with enough events for reliability.
    Returns interpolated daily curve from day 365 to day 0.
    """
    # Filter reliable buckets
    reliable = curves[curves['n_events'] >= min_events].copy()
    reliable = reliable.sort_values('day_bucket')

    if len(reliable) < 3:
        return pd.DataFrame()

    # Interpolate to daily resolution
    day_range = range(0, int(reliable['day_bucket'].max()) + 1)
    interp = pd.DataFrame({'day': day_range})

    interp['price_index'] = np.interp(
        interp['day'],
        reliable['day_bucket'],
        reliable['median_price_index']
    )
    interp['p25_index'] = np.interp(
        interp['day'],
        reliable['day_bucket'],
        reliable['p25_price_index']
    )
    interp['p75_index'] = np.interp(
        interp['day'],
        reliable['day_bucket'],
        reliable['p75_price_index']
    )
    interp['n_events'] = np.interp(
        interp['day'],
        reliable['day_bucket'],
        reliable['n_events']
    ).astype(int)

    # Apply light smoothing (7-day rolling)
    interp['price_index_smooth'] = (
        interp['price_index']
          .rolling(window=7, center=True, min_periods=1)
          .median()
    )

    return interp.sort_values('day', ascending=False).reset_index(drop=True)


# ================================================================== #
#  STEP 3: PREDICT PRICE                                               #
# ================================================================== #
def predict_price(event_id: int,
                  current_price: float,
                  days_before_event: int,
                  target_days: int = None) -> dict:
    """
    Given current market price and days before event,
    predict price at target_days (or range of future windows).

    current_price: current listing price (absolute $)
    days_before_event: how many days until the event right now
    target_days: specific future day to predict (optional)
    """
    curves = load_curves(event_id)
    trajectory = fit_trajectory(curves)

    if trajectory.empty:
        return {'error': 'Insufficient trajectory data'}

    # Get current price index at current day
    current_row = trajectory[
        trajectory['day'] == days_before_event
    ]
    if current_row.empty:
        # Find nearest day
        trajectory['dist'] = abs(
            trajectory['day'] - days_before_event
        )
        current_row = trajectory.nsmallest(1, 'dist')

    current_index = float(current_row['price_index_smooth'].iloc[0])

    # Infer baseline price from current price and index
    if current_index > 0:
        baseline_price = current_price / current_index
    else:
        baseline_price = current_price

    predictions = {}

    # Predict at specific windows
    target_windows = (
        [target_days] if target_days
        else [90, 60, 30, 14, 7, 0]
    )

    for target_day in target_windows:
        if target_day >= days_before_event:
            continue  # Can't predict past

        target_row = trajectory[trajectory['day'] == target_day]
        if target_row.empty:
            trajectory['dist'] = abs(trajectory['day'] - target_day)
            target_row = trajectory.nsmallest(1, 'dist')

        pred_index  = float(target_row['price_index_smooth'].iloc[0])
        pred_p25    = float(target_row['p25_index'].iloc[0])
        pred_p75    = float(target_row['p75_index'].iloc[0])
        n_events    = int(target_row['n_events'].iloc[0])

        pred_price  = baseline_price * pred_index
        pred_low    = baseline_price * pred_p25
        pred_high   = baseline_price * pred_p75

        change_pct  = ((pred_price - current_price) / current_price) * 100

        predictions[target_day] = {
            'predicted_price':  round(pred_price, 2),
            'low_estimate':     round(pred_low, 2),
            'high_estimate':    round(pred_high, 2),
            'change_vs_now':    round(change_pct, 1),
            'price_index':      round(pred_index, 3),
            'n_events_support': n_events,
        }

    return {
        'event_id':       event_id,
        'current_price':  current_price,
        'days_remaining': days_before_event,
        'baseline_price': round(baseline_price, 2),
        'current_index':  round(current_index, 3),
        'predictions':    predictions,
    }


# ================================================================== #
#  STEP 4: SELL RECOMMENDATION ENGINE                                  #
# ================================================================== #
def recommend_sell_timing(event_id: int,
                          current_price: float,
                          days_before_event: int,
                          cost_basis: float = None) -> dict:
    """
    Given current position, recommend:
    - SELL NOW: current price is near/above predicted peak
    - HOLD:     price expected to appreciate
    - MONITOR:  uncertain — check again in N days
    - LIQUIDATE: price declining, cut losses

    cost_basis: what you paid (optional, enables P&L framing)
    """
    curves  = load_curves(event_id)
    traj    = fit_trajectory(curves)

    if traj.empty:
        return {'recommendation': 'INSUFFICIENT_DATA'}

    # Get full future trajectory from now
    future = traj[traj['day'] <= days_before_event].copy()
    if future.empty:
        return {'recommendation': 'INSUFFICIENT_DATA'}

    # Current index
    current_row   = future[future['day'] == days_before_event]
    if current_row.empty:
        future['dist'] = abs(future['day'] - days_before_event)
        current_row = future.nsmallest(1, 'dist')
    current_index = float(current_row['price_index_smooth'].iloc[0])

    # Infer baseline
    baseline = current_price / current_index if current_index > 0 \
               else current_price

    # Find peak in remaining window
    # Find peak in FUTURE only (days strictly below current position)
    future['abs_price'] = future['price_index_smooth'] * baseline
    future_only = future[future['day'] < days_before_event].copy()

    if future_only.empty:
        peak_price = current_price
        peak_day   = days_before_event
    else:
        peak_row   = future_only.loc[future_only['abs_price'].idxmax()]
        peak_price = float(peak_row['abs_price'])
        peak_day   = int(peak_row['day'])

    # Find trough in future only
    if future_only.empty:
        trough_price = current_price
    else:
        trough_row   = future_only.loc[future_only['abs_price'].idxmin()]
        trough_price = float(trough_row['abs_price'])

    # Day 0 price
    day0_row    = future[future['day'] == 0]
    if day0_row.empty:
        day0_row = future.nsmallest(1, 'day')
    day0_price  = float(day0_row['abs_price'].iloc[0])

    # Upside / downside from current
    upside_pct   = ((peak_price - current_price) / current_price) * 100
    downside_pct = ((trough_price - current_price) / current_price) * 100
    day0_pct     = ((day0_price - current_price) / current_price) * 100

    # P&L vs cost basis
    pnl = {}
    if cost_basis:
        pnl = {
            'cost_basis':        cost_basis,
            'current_profit_pct': round(
                ((current_price - cost_basis) / cost_basis) * 100, 1
            ),
            'peak_profit_pct':   round(
                ((peak_price - cost_basis) / cost_basis) * 100, 1
            ),
        }

    # Decision logic
    # Data support note for reasoning
    max_support = int(curves['n_events'].max())
    support_note = f"Supported by {max_support} historical comp events."

    # Confidence band width — flag if wide
    band_widths = curves['p75_price_index'] - curves['p25_price_index']
    avg_band = float(band_widths.mean())
    uncertainty_note = (
        " ⚠️ High price variability in comps — treat as directional only."
        if avg_band > 0.8 else ""
    )

    # Risk/reward ratio — if downside is more than 2x the upside, lean toward sell
    risk_reward_ratio = abs(downside_pct) / max(upside_pct, 0.1)
    poor_risk_reward  = risk_reward_ratio > 2.5 and downside_pct < -15

    # Decision logic
    if poor_risk_reward and upside_pct < 20:
        recommendation = 'SELL_NOW'
        reasoning = (
            f"Unfavorable risk/reward — potential upside of "
            f"+{max(upside_pct, 0):.1f}% does not justify downside risk of "
            f"{downside_pct:.1f}%. Comp trajectories show price decay "
            f"from this point toward ${day0_price:.0f} at event day. "
            f"Selling now protects current value. "
            f"{support_note}{uncertainty_note}"
        )
        action_day = days_before_event

    elif upside_pct >= 15 and peak_day > 7:
        recommendation = 'HOLD'
        hold_days = days_before_event - peak_day
        timing_note = (
            f" Note: Peak window is only {hold_days} days away — act quickly."
            if hold_days <= 21 else ""
        )
        reasoning = (
            f"Price trajectory shows appreciation to ${peak_price:.0f} "
            f"(+{upside_pct:.1f}%) around {peak_day} days before event. "
            f"Current price is below the historical comp peak window. "
            f"{support_note}{uncertainty_note}{timing_note}"
        )
        action_day = peak_day

    elif upside_pct >= 5 and peak_day > 14:
        recommendation = 'HOLD'
        hold_days = days_before_event - peak_day
        timing_note = (
            f" Note: Peak window is only {hold_days} days away — act quickly."
            if hold_days <= 21 else ""
        )
        reasoning = (
            f"Modest upside expected — comps show prices reaching "
            f"${peak_price:.0f} (+{upside_pct:.1f}%) at {peak_day} days out. "
            f"Downside limited to {downside_pct:.1f}%. "
            f"{support_note}{uncertainty_note}{timing_note}"
        )
        action_day = peak_day

    elif downside_pct < -15 and upside_pct < 5:
        recommendation = 'SELL_NOW'
        reasoning = (
            f"Comp trajectories show consistent price decay from this point — "
            f"expected range ${trough_price:.0f} to ${peak_price:.0f} going forward, "
            f"with day-of price around ${day0_price:.0f} ({day0_pct:.1f}% vs now). "
            f"Selling now locks in current value. {support_note}{uncertainty_note}"
        )
        action_day = days_before_event

    elif abs(upside_pct) < 5 and abs(downside_pct) < 10:
        recommendation = 'MONITOR'
        reasoning = (
            f"Flat trajectory — comps show little price movement from this point. "
            f"Upside {upside_pct:.1f}%, downside {downside_pct:.1f}%. "
            f"No urgency to sell or hold. Re-evaluate in 14 days. "
            f"{support_note}{uncertainty_note}"
        )
        action_day = days_before_event - 14

    elif day0_pct < -10 and upside_pct < 10:
        recommendation = 'SELL_NOW'
        reasoning = (
            f"Historical comps show price decays to ~${day0_price:.0f} "
            f"by event day ({day0_pct:.1f}% below current). "
            f"Limited upside ({upside_pct:.1f}%) doesn't justify the hold risk. "
            f"{support_note}{uncertainty_note}"
        )
        action_day = days_before_event

    else:
        recommendation = 'MONITOR'
        reasoning = (
            f"Mixed signals — upside {upside_pct:.1f}%, "
            f"downside {downside_pct:.1f}%, day-of {day0_pct:.1f}%. "
            f"Review again in 21 days as event approaches. "
            f"{support_note}{uncertainty_note}"
        )
        action_day = max(0, days_before_event - 21)

    return {
        'recommendation':   recommendation,
        'reasoning':        reasoning,
        'action_day':       action_day,
        'current_price':    round(current_price, 2),
        'peak_price':       round(peak_price, 2),
        'peak_day':         peak_day,
        'trough_price':     round(trough_price, 2),
        'day0_price':       round(day0_price, 2),
        'upside_pct':       round(upside_pct, 1),
        'downside_pct':     round(downside_pct, 1),
        'day0_pct':         round(day0_pct, 1),
        **pnl
    }


# ================================================================== #
#  STEP 5: PORTFOLIO ANALYSIS (multiple tickets)                       #
# ================================================================== #
def analyze_portfolio(holdings: list) -> pd.DataFrame:
    """
    Analyze multiple ticket holdings at once.
    holdings: list of dicts with keys:
        event_id, event_name, current_price,
        days_before_event, quantity, cost_basis (optional)
    """
    results = []
    for h in holdings:
        try:
            rec = recommend_sell_timing(
                event_id           = h['event_id'],
                current_price      = h['current_price'],
                days_before_event  = h['days_before_event'],
                cost_basis         = h.get('cost_basis')
            )
            results.append({
                'event_name':      h.get('event_name', h['event_id']),
                'event_id':        h['event_id'],
                'quantity':        h.get('quantity', 1),
                'current_price':   h['current_price'],
                'days_remaining':  h['days_before_event'],
                'recommendation':  rec['recommendation'],
                'peak_price':      rec['peak_price'],
                'peak_day':        rec['peak_day'],
                'upside_pct':      rec['upside_pct'],
                'downside_pct':    rec['downside_pct'],
                'day0_price':      rec['day0_price'],
                'reasoning':       rec['reasoning'],
            })
        except Exception as e:
            results.append({
                'event_name':     h.get('event_name', h['event_id']),
                'recommendation': f'ERROR: {e}'
            })

    df = pd.DataFrame(results)
    if not df.empty and 'recommendation' in df.columns:
        # Sort by priority: SELL_NOW first, then HOLD, then MONITOR
        priority = {'SELL_NOW': 0, 'HOLD': 1, 'MONITOR': 2}
        df['priority'] = df['recommendation'].map(
            lambda x: priority.get(x, 3)
        )
        df = df.sort_values('priority').drop('priority', axis=1)
    return df


# ================================================================== #
#  RUN TEST                                                            #
# ================================================================== #
if __name__ == "__main__":

    print("\n" + "="*65)
    print("PRICE PREDICTION & SELL RECOMMENDATION ENGINE")
    print("="*65)

    # Test cases — simulated current positions
    test_cases = [
        {
            'event_id':          5687566,
            'event_name':        'NCAA Final Four Championship',
            'current_price':     850,
            'days_before_event': 180,
            'quantity':          2,
            'cost_basis':        600,
        },
        {
            'event_id':          6487949,
            'event_name':        'NFL Ravens vs Chargers',
            'current_price':     450,
            'days_before_event': 120,
            'quantity':          4,
            'cost_basis':        380,
        },
        {
            'event_id':          5045286,
            'event_name':        'US Open Golf Friday',
            'current_price':     900,
            'days_before_event': 250,
            'quantity':          2,
            'cost_basis':        750,
        },
        {
            'event_id':          6338949,
            'event_name':        'Zach Bryan - Gillette Stadium',
            'current_price':     280,
            'days_before_event': 150,
            'quantity':          4,
            'cost_basis':        200,
        },
        {
            'event_id':          6062329,
            'event_name':        'My Chemical Romance',
            'current_price':     500,
            'days_before_event': 200,
            'quantity':          2,
            'cost_basis':        350,
        },
    ]

    # Individual detailed predictions
    for case in test_cases:
        print(f"\n{'#'*65}")
        print(f"# {case['event_name']}")
        print(f"{'#'*65}")

        # Price predictions at future windows
        pred = predict_price(
            event_id           = case['event_id'],
            current_price      = case['current_price'],
            days_before_event  = case['days_before_event'],
        )

        print(f"\nCURRENT POSITION:")
        print(f"  Price:          ${case['current_price']}")
        print(f"  Days remaining: {case['days_before_event']}")
        print(f"  Cost basis:     ${case.get('cost_basis', 'N/A')}")
        print(f"  Quantity:       {case.get('quantity', 1)}")
        print(f"  Baseline price: ${pred.get('baseline_price', 'N/A')}")

        print(f"\nPRICE PREDICTIONS:")
        for day, data in sorted(
            pred.get('predictions', {}).items(), reverse=True
        ):
            direction = "↑" if data['change_vs_now'] > 0 else "↓"
            print(f"  At {day:>3} days: "
                  f"${data['predicted_price']:>7.2f} "
                  f"{direction}{abs(data['change_vs_now']):.1f}% "
                  f"[${data['low_estimate']:.0f}-${data['high_estimate']:.0f}] "
                  f"({data['n_events_support']} events)")

        # Sell recommendation
        rec = recommend_sell_timing(
            event_id           = case['event_id'],
            current_price      = case['current_price'],
            days_before_event  = case['days_before_event'],
            cost_basis         = case.get('cost_basis'),
        )

        print(f"\nRECOMMENDATION: {rec['recommendation']}")
        print(f"  {rec['reasoning']}")
        if 'current_profit_pct' in rec:
            print(f"  Current P&L:  {rec['current_profit_pct']:+.1f}%")
            print(f"  Peak P&L:     {rec['peak_profit_pct']:+.1f}%")

    # Portfolio summary
    print(f"\n{'='*65}")
    print("PORTFOLIO SUMMARY")
    print(f"{'='*65}")
    portfolio = analyze_portfolio(test_cases)
    print(portfolio[[
        'event_name', 'days_remaining', 'current_price',
        'recommendation', 'upside_pct', 'downside_pct',
        'peak_price', 'peak_day'
    ]].to_string(index=False))