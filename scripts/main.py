#!/usr/bin/env python3
"""
TicketCity Ticket Pricing Analyzer
====================================
Single script — enter an EventID, get a full analysis.
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from google.cloud import bigquery
import re

# ================================================================== #
#  SETUP                                                               #
# ================================================================== #
client = bigquery.Client(project="ticketcity-tcg")
TODAY  = datetime.now(timezone.utc).strftime('%Y-%m-%d')
TRAJECTORIES_DIR = Path("data/trajectories")
TRAJECTORIES_DIR.mkdir(parents=True, exist_ok=True)
Path("data").mkdir(exist_ok=True)

def _get_vivid_partition() -> str:
    """Return the most recent loaded VividIntake partition date (YYYY-MM-DD).
    Uses a free INFORMATION_SCHEMA metadata query — no table scan."""
    result = client.query("""
        SELECT partition_id
        FROM `data-ticketcity.VividIntake.INFORMATION_SCHEMA.PARTITIONS`
        WHERE table_name = 'Events'
          AND partition_id NOT IN ('__NULL__', '__UNPARTITIONED__')
        ORDER BY partition_id DESC
        LIMIT 1
    """).result()
    row = next(iter(result), None)
    if row:
        pid = str(row[0])
        return f"{pid[:4]}-{pid[4:6]}-{pid[6:8]}"
    # Fallback: yesterday in case today's partition isn't loaded yet
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')

PARTITION = _get_vivid_partition()

# ================================================================== #
#  CATEGORY ROUTING                                                    #
# ================================================================== #
ANNUAL_CATEGORIES = [
    'PGA Golf', 'NASCAR Racing', 'F1 Racing', 'Tennis',
    'Horse Racing', 'Motorsports'
]
SPORTS_CATEGORIES = [
    'NFL Football', 'NCAA Basketball', 'NBA Basketball', 'MLB Baseball',
    'NHL Hockey', 'NCAA Football', 'NCAA Womens Basketball', 'MLS',
    'Soccer', 'Boxing and Fighting', 'Wrestling', 'Arena Football',
    'College Baseball', 'Rugby', 'Lacrosse', 'Volleyball', 'Basketball',
    'Football', 'Hockey', 'Baseball'
]
CONCERT_CATEGORIES = [
    'Rock', 'Pop', 'Country and Folk', 'Rap/Hip Hop', 'R&B',
    'Alternative', 'Hard Rock', 'Blues and Jazz', 'Classical',
    'Latin Music', 'K-pop', 'Reggae', 'World Music', 'Adult Contemporary',
    'Dance/Electronica', 'Music Festivals', 'Other Concerts'
]
THEATER_CATEGORIES = [
    'Broadway', 'Arts and Theater', 'Comedy', 'Musical', 'Opera',
    'Ballet and Dance', 'Cirque', 'Circus', 'Family', 'Off-Broadway',
    'Other Theater', 'Magic', 'Public Speaking', 'Podcast'
]

# ================================================================== #
#  EXCLUSIONS                                                          #
# ================================================================== #
UNIVERSAL_EXCLUSIONS = [
    '%Test%', '%arking%', '%Gift Card%', '%eGift%',
    '%Voucher%', '%Package%',
]
COVID_EXCLUSIONS = [
    '%distanc%', '%Reduced Capacity%', '%Social Distance%',
    '%Limited Capacity%', '%No Fans%', '%Capacity Restricted%'
]
MENS_EXCLUSIONS = [
    '%Women%', '%Womens%', '%Womens %', '%WNBA%', '%Girls%'
]
WOMENS_EXCLUSIONS = [
    '%Mens%', '%Mens %', '%Boys%'
]
BUNDLE_EXCLUSIONS = [
    '%All Session%', '%All-Session%', '%Full Season%',
    '%Season Pass%', '%Mini Plan%', '%Flex Plan%',
    '%Multiple%'
]
AMATEUR_EXCLUSIONS = [
    '%School%', '%Amateur%', '%Community%',
    '%University Production%', '%High School%',
    '%Youth%', '%Student%', '%Academy%'
]
TRIBUTE_EXCLUSIONS = [
    '%Tribute%', '%tribute%', '%Salute to%',
    '%Celebration of%'
]
PRESEASON_EXCLUSIONS = [
    '%Preseason%', '%Pre-Season%', '%Pre Season%',
    '%Exhibition%', '%Scrimmage%', '%Spring Training%',
    '%Training Camp%'
]
PLAYOFF_EXCLUSIONS = [
    '%Playoff%', '%Wild Card%', '%Divisional%',
    '%Conference Championship%', '%Super Bowl%',
    '%World Series%', '%Stanley Cup%', '%NBA Finals%',
    '%Final Four%', '%Championship Game%'
]
PRACTICE_ROUND_EXCLUSIONS = [
    '%Practice%', '%Pro-Am%', '%Pro Am%',
    '%Qualifier%', '%Qualifying%'
]
GOLF_WEEKDAY_EXCLUSIONS = [
    '%Monday%', '%Tuesday%', '%Wednesday%'
]

# ================================================================== #
#  TOURNAMENT TIERS                                                    #
# ================================================================== #
TOURNAMENT_TIERS = {
    'championship': [
        'Final Four', 'Championship Game', 'Championship -',
        'Super Bowl', 'World Series', 'Stanley Cup Final',
        'NBA Finals', 'College Football Playoff National',
        'MLS Cup', 'Tour Championship', 'Ryder Cup',
        'National Championship'
    ],
    'semifinal': [
        'Semifinal', 'Semi-Final', 'Conference Championship',
        'League Championship', 'ALCS', 'NLCS', 'Conference Finals',
        'Elite Eight', 'Elite 8', 'Final Four'
    ],
    'quarterfinal': [
        'Sweet Sixteen', 'Sweet 16', 'Divisional',
        'Division Series', 'ALDS', 'NLDS', 'Regional'
    ],
    'tournament_early': [
        'Tournament', 'Wild Card', 'First Round',
        'Second Round', 'Bowl Game', 'Playoff'
    ]
}

GOLF_ROUNDS = {
    'weekend': ['Saturday', 'Sunday', 'Final Round', 'Third Round'],
    'weekday': ['Thursday', 'Friday', 'First Round', 'Second Round'],
    'practice': ['Monday', 'Tuesday', 'Wednesday', 'Pro-Am', 'Practice']
}

# ================================================================== #
#  THRESHOLDS                                                          #
# ================================================================== #
MIN_ORDERS = {
    'sports':  30,
    'concert': 20,
    'theater': 10,
    'annual':  5,
    'generic': 10
}

VENUE_TIER_RANGES = {
    'stadium':     (2000, float('inf')),
    'large_arena': (800,  2000),
    'arena':       (300,  800),
    'theater':     (100,  300),
    'small':       (0,    100)
}

RECENCY_CUTOFF = '2024-10-01'

# ================================================================== #
#  HELPERS                                                             #
# ================================================================== #
def build_exclusions(patterns):
    safe = [p.replace("'", "''") for p in patterns]
    return "\n".join([f"AND Name NOT LIKE '{p}'" for p in safe])

def get_venue_tier(listing_count):
    for tier, (lo, hi) in VENUE_TIER_RANGES.items():
        if lo <= listing_count < (hi if hi != float('inf') else 999999):
            return tier
    return 'small'

def detect_mode(category):
    if category in ANNUAL_CATEGORIES:   return 'annual'
    if category in SPORTS_CATEGORIES:   return 'sports'
    if category in CONCERT_CATEGORIES:  return 'concert'
    if category in THEATER_CATEGORIES:  return 'theater'
    return 'generic'

def detect_tournament_tier(name):
    name_lower = name.lower()
    for tier, keywords in TOURNAMENT_TIERS.items():
        for kw in keywords:
            if kw.lower() in name_lower:
                return tier, kw
    return None, None

def detect_golf_round(name):
    name_lower = name.lower()
    for round_type, keywords in GOLF_ROUNDS.items():
        for kw in keywords:
            if kw.lower() in name_lower:
                return round_type
    return 'weekday'

def detect_gender(name):
    name_lower = name.lower()
    if any(w in name_lower for w in ['womens', 'wnba', 'girls']):
        return 'womens'
    if any(w in name_lower for w in ['mens ', "men's"]):
        return 'mens'
    return 'neutral'

def detect_sport_subtype(name):
    preseason_kws = ['preseason', 'pre-season', 'exhibition',
                     'spring training', 'scrimmage']
    if any(kw in name.lower() for kw in preseason_kws):
        return 'preseason'
    tier, _ = detect_tournament_tier(name)
    return tier if tier else 'regular'

def parse_event_date(date_str):
    if not date_str:
        return None
    for fmt in ['%m/%d/%y %I:%M %p', '%m/%d/%Y %I:%M %p']:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None

def safe_sql_str(s: str) -> str:
    """
    Escape a Python string for safe use inside a BigQuery SQL string literal.
    """
    return str(s or "").replace("\\", "\\\\").replace("'", "\\'")


def classify_ticket_class(section_name: str, row_name: str = "") -> str:
    """
    Broad ticket class required for deliverable segmentation.
    Returns GA or RESERVED.
    """
    s = f"{section_name or ''} {row_name or ''}".upper()


    ga_terms = [
    "GA", "GENERAL ADMISSION", "STANDING", "LAWN", "SRO"
]
    if any(term in s for term in ga_terms):
        return "GA"
    return "RESERVED"


def pct_change_safe(old_val, new_val):
    """
    Safe percent change helper.
    """
    if old_val is None or pd.isna(old_val) or old_val == 0:
        return np.nan
    if new_val is None or pd.isna(new_val):
        return np.nan
    return (new_val - old_val) / old_val * 100.0


def classify_market_behavior(
    price_change_14d: float,
    ticket_change_14d: float,
    listing_change_14d: float,
) -> str:
    """
    Label a section based on recent supply + price movement.
    """
    if pd.notna(price_change_14d) and pd.notna(ticket_change_14d):
        # Prices falling while supply barely clears = decay risk
        if price_change_14d <= -10 and ticket_change_14d >= -5:
            return "DECAY_RISK"

        # Price stable/rising while supply clears = reliable sell-through
        if price_change_14d >= 0 and ticket_change_14d <= -15:
            return "RELIABLE"

    if pd.notna(price_change_14d) and pd.notna(listing_change_14d):
        if price_change_14d <= -8 and listing_change_14d >= -5:
            return "DECAY_RISK"

    return "BALANCED"

# ================================================================== #
#  STEP 1: EVENT INFO                                                  #
# ================================================================== #
def get_event_info(event_id: int) -> dict:
    query = f"""
    SELECT
        e.EventID,
        e.Name,
        e.LocalDate,
        e.VenueID,
        e.EventType,
        e.TicketCount,
        e.ListingCount,
        e.MinPrice,
        e.MaxPrice,
        e.CategoryID,
        ep.Performer_ID,
        ep.Master,
        p.Performer_Name,
        c.Category_Name,
        v.Venue_Name,
        v.City,
        v.State
    FROM `data-ticketcity.VividIntake.Events` e
    LEFT JOIN `data-ticketcity.VividIntake.EventPerformers` ep
        ON e.EventID = ep.Event_ID
        AND DATE(ep._PARTITIONTIME) = DATE(e._PARTITIONTIME)
    LEFT JOIN `data-ticketcity.VividIntake.Performers` p
        ON ep.Performer_ID = p.Performer_ID
        AND DATE(p._PARTITIONTIME) = DATE(e._PARTITIONTIME)
    LEFT JOIN `data-ticketcity.VividIntake.Categories` c
        ON e.CategoryID = c.Category_ID
        AND DATE(c._PARTITIONTIME) = DATE(e._PARTITIONTIME)
    LEFT JOIN `data-ticketcity.VividIntake.Venues` v
        ON e.VenueID = v.Venue_ID
        AND DATE(v._PARTITIONTIME) = DATE(e._PARTITIONTIME)
    WHERE e.EventID = {event_id}
      AND DATE(e._PARTITIONTIME) = DATE("{PARTITION}")
    ORDER BY e._PARTITIONTIME DESC
    LIMIT 20
    """
    df = client.query(query).to_dataframe()

    if df.empty:
        return None

    row = df.iloc[0]
    performers_df = (
        df[['Performer_ID', 'Master', 'Performer_Name']]
        .dropna(subset=['Performer_ID'])
        .drop_duplicates(subset=['Performer_ID'])
    )

    return {
        'EventID': int(event_id),
        'Name': str(row['Name'] or ''),
        'LocalDate': str(row['LocalDate'] or ''),
        'VenueID': int(row['VenueID'] or 0),
        'Venue_Name': str(row['Venue_Name'] or ''),
        'City': str(row['City'] or ''),
        'State': str(row['State'] or ''),
        'EventType': str(row['EventType'] or ''),
        'TicketCount': int(row['TicketCount'] or 0),
        'ListingCount': int(row['ListingCount'] or 0),
        'MinPrice': float(row['MinPrice'] or 0),
        'MaxPrice': float(row['MaxPrice'] or 0),
        'CategoryID': int(row['CategoryID'] or 0),
        'Category_Name': str(row['Category_Name'] or ''),
        'performers_df': performers_df,
    }


# ================================================================== #
#  STEP 2: CURRENT MARKET PRICE                                        #
# ================================================================== #
def get_current_market_price(event_id: int) -> dict:
    """Most recent sale data from 75_Event_Daily."""
    query = f"""
    SELECT
        PID,
        Date                AS EventDate,
        Report_Date,
        Orders,
        Avg_Order_Size,
        Revenue,
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
        'avg_order_size':    float(latest['Avg_Order_Size']),
        'orders':            int(latest['Orders']),
        'days_before_event': (int(latest['days_before_event'])
                              if pd.notna(latest['days_before_event'])
                              else None),
        'report_date':       str(latest['Report_Date']),
        'recent_history':    df,
    }

def get_ticketcity_market_share(event_id: int, info: dict) -> dict:
    """
    Query TicketCity_Tickets for this event and compare to
    total market listings from VividIntake.
    Returns TicketCity's share of listings and ticket count.
    """
    query = f"""
    SELECT
        COUNT(*) AS tc_blocks,
        SUM(CAST(JSON_EXTRACT_SCALAR(quantity, '$') AS INT64))
            AS tc_tickets
    FROM `data-ticketcity.TC_Data.TicketCity_Tickets`
    WHERE tfs_event_id = {event_id}
      OR CAST(tfs_event_id AS STRING) = '{event_id}'
    """
    try:
        df = client.query(query).to_dataframe()
    except Exception:
        return None

    if df.empty or df.iloc[0]['tc_blocks'] == 0:
        # Try matching via axis_event_id as fallback
        return None

    tc_blocks   = int(df.iloc[0]['tc_blocks']  or 0)
    tc_tickets  = int(df.iloc[0]['tc_tickets'] or 0)

    total_listings = int(info.get('ListingCount', 0))
    total_tickets  = int(info.get('TicketCount',  0))

    listing_share = round(tc_blocks  / total_listings * 100, 1) \
                    if total_listings > 0 else None
    ticket_share  = round(tc_tickets / total_tickets  * 100, 1) \
                    if total_tickets  > 0 else None

    return {
        'tc_blocks':      tc_blocks,
        'tc_tickets':     tc_tickets,
        'total_listings': total_listings,
        'total_tickets':  total_tickets,
        'listing_share':  listing_share,
        'ticket_share':   ticket_share,
    }

# ================================================================== #
#  STEP 3: PERFORMER LOOKUP FOR CONCERTS                               #
# ================================================================== #
def get_performer_event_ids_from_daily(performer_names: list) -> list:
    if not performer_names:
        return []

    name_conditions = " OR ".join([
        f"LOWER(Name) LIKE '%{n.lower().replace(chr(39), chr(39)*2)}%'"
        for n in performer_names
    ])

    query = f"""
    SELECT DISTINCT PID
    FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
    WHERE ({name_conditions})
      AND Name NOT LIKE '%Test%'
      AND Name NOT LIKE '%arking%'
      AND Name NOT LIKE '%Tribute%'
      AND Name NOT LIKE '%tribute%'
      AND Name NOT LIKE '%School%'
      AND Report_Date < '{TODAY}'
    LIMIT 500
    """
    df = client.query(query).to_dataframe()
    pids = df['PID'].tolist()
    print(f"  → Performer lookup: {len(pids)} historical events found")
    return pids


# ================================================================== #
#  STEP 4: COMP ENGINE                                                 #
# ================================================================== #
def evaluate_confidence(comps, venue_tier, mode, target_listings):
    if comps is None or comps.empty:
        return 'NONE', 'No comps found'

    best_score        = comps['similarity_score'].max()
    n_comps           = len(comps)
    avg_orders        = comps['total_orders'].mean()
    max_comp_listings = comps.get(
        'total_quantity', pd.Series([0])
    ).max()

    notes = (f"Best score: {best_score} | "
             f"Comps: {n_comps} | "
             f"Avg orders: {avg_orders:.0f}")

    warnings = []
    if max_comp_listings > 0 and target_listings > max_comp_listings * 2:
        warnings.append(
            f"⚠️  SCALE OUTLIER: Target has {target_listings:.0f} listings "
            f"vs comp max {max_comp_listings:.0f} — predictions less reliable"
        )
    if venue_tier in ['stadium', 'large_arena'] and mode == 'theater':
        warnings.append(
            "⚠️  OUTLIER: Arena-scale theater/comedy — limited comp pool"
        )

    if warnings:
        notes += '\n' + '\n'.join(warnings)

    if best_score >= 8 and n_comps >= 8:
        level = 'HIGH'
    elif best_score >= 5 and n_comps >= 4:
        level = 'MEDIUM'
    else:
        level = 'LOW'

    if any('OUTLIER' in w for w in warnings) and level == 'HIGH':
        level = 'MEDIUM'

    return level, notes


def theater_fallback_comps(p, show_name_clean, min_price,
                           listing_low, listing_high,
                           excl_sql, min_orders, n_comps):
    base_name = re.split(
        r' - | in Concert| Live| The Musical', show_name_clean
    )[0].strip()

    tiers = [
        ('Show base name',
         f"AND LOWER(Name) LIKE '%{base_name.lower()}%'",
         f"AND Category = '{p['Category_Name']}'"),
        ('Same venue + price tier',
         '',
         f"AND Category = '{p['Category_Name']}' "
         f"AND Venue = '{p['Venue_Name'].replace(chr(39), chr(39)*2)}'"),
        ('Same category + price tier',
         '',
         f"AND Category = '{p['Category_Name']}'"),
    ]

    for label, name_filter, cat_filter in tiers:
        print(f"  → Theater fallback: trying '{label}'")
        query = f"""
        WITH raw AS (
            SELECT
                PID,
                Name,
                Date            AS EventDate,
                Venue, City, State, Category,
                SUM(Revenue)        AS total_revenue,
                SUM(Orders)         AS total_orders,
                SUM(Quantity)       AS total_quantity,
                AVG(Avg_Order_Size) AS avg_order_size,
                MIN(Report_Date)    AS first_report,
                MAX(Report_Date)    AS last_report
            FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
            WHERE Report_Date BETWEEN '2021-01-01' AND '{TODAY}'
              AND PID != {int(p['EventID'])}
              {excl_sql}
              {name_filter}
              {cat_filter}
            GROUP BY PID, Name, Date, Venue, City, State, Category
            HAVING SUM(Orders) >= {min_orders}
        ),
        scored AS (
            SELECT
                PID AS EventID, Name, EventDate, Venue, City, State,
                Category, total_revenue, total_orders, total_quantity,
                avg_order_size, first_report, last_report,
                (
                    CASE WHEN avg_order_size BETWEEN {min_price*0.3}
                                                 AND {min_price*4.0}
                         THEN 4 ELSE 0 END +
                    CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                                 AND {listing_high*3.0}
                         THEN 2 ELSE 0 END +
                    CASE WHEN total_orders > 50 THEN 2 ELSE 0 END +
                    CASE WHEN last_report >= '{RECENCY_CUTOFF}'
                         THEN 2 ELSE 0 END +
                    CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
                ) AS similarity_score
            FROM raw
        ),
        deduped AS (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY EventID
                    ORDER BY similarity_score DESC, total_orders DESC
                ) AS rn
            FROM scored
        )
        SELECT * EXCEPT(rn)
        FROM deduped
        WHERE rn = 1
        ORDER BY similarity_score DESC, total_orders DESC
        LIMIT {n_comps}
        """
        df = client.query(query).to_dataframe()
        if len(df) >= 3:
            print(f"  → Found {len(df)} comps via '{label}'")
            return df, label

    return pd.DataFrame(), 'none'


def find_comps(event_id: int,
               info: dict,
               n_comps: int = 15,
               market_price: float = None) -> tuple:

    p            = info
    performers   = p['performers_df']
    category     = p['Category_Name']
    name         = p['Name']
    min_price    = p['MinPrice']
    max_price    = p['MaxPrice']
    listings     = p['ListingCount']
    mode         = detect_mode(category)
    venue_tier   = get_venue_tier(listings)
    gender       = detect_gender(name)
    sport_sub    = detect_sport_subtype(name)
    tour_tier, _ = detect_tournament_tier(name)
    golf_round   = detect_golf_round(name) if mode == 'annual' else None

    masters      = performers[performers['Master'] == True]
    supporting   = performers[performers['Master'] == False]
    master_names = masters['Performer_Name'].tolist()
    away_names   = supporting['Performer_Name'].tolist()

    venue_range  = VENUE_TIER_RANGES[venue_tier]
    listing_low  = venue_range[0]
    listing_high = (venue_range[1]
                    if venue_range[1] != float('inf') else 999999)
    min_orders   = MIN_ORDERS[mode]

    # Price anchor — use market price if available, else MinPrice
    price_anchor = market_price if market_price else min_price

    print(f"\n{'='*65}")
    print(f"TARGET:      {name}")
    print(f"Category:    {category} | Mode: {mode.upper()}")
    print(f"Venue Tier:  {venue_tier} ({listings:.0f} listings)")
    print(f"Gender:      {gender} | Subtype: {sport_sub}")
    print(f"Venue:       {p['Venue_Name']}, {p['City']}, {p['State']}")
    print(f"Date:        {p['LocalDate']}")
    print(f"Price Range: ${min_price} – ${max_price}")
    if market_price:
        print(f"Market Price:${market_price:.2f} (recent sale)")
    print(f"Headliners:  {master_names}")
    if tour_tier:  print(f"Tourney Tier:{tour_tier}")
    if golf_round: print(f"Golf Round:  {golf_round}")
    print(f"{'='*65}")

    # ── Exclusions ───────────────────────────────────────────────── #
    exclusions = (list(UNIVERSAL_EXCLUSIONS) +
                  list(COVID_EXCLUSIONS) +
                  list(BUNDLE_EXCLUSIONS))

    if mode == 'sports':
        if gender == 'mens':
            exclusions += MENS_EXCLUSIONS
        elif gender == 'womens':
            exclusions += WOMENS_EXCLUSIONS
        if sport_sub in ['regular', 'championship', 'semifinal',
                         'quarterfinal', 'tournament_early']:
            exclusions += PRESEASON_EXCLUSIONS
        if sport_sub == 'regular':
            exclusions += PLAYOFF_EXCLUSIONS

    if mode in ['concert', 'theater']:
        exclusions += AMATEUR_EXCLUSIONS
        exclusions += TRIBUTE_EXCLUSIONS

    if mode == 'annual' and golf_round in ['weekend', 'weekday']:
        exclusions += PRACTICE_ROUND_EXCLUSIONS
    if mode == 'annual' and golf_round == 'weekday':
        exclusions += GOLF_WEEKDAY_EXCLUSIONS

    excl_sql = build_exclusions(exclusions)

    # ── Mode-specific scoring & filters ─────────────────────────── #
    name_filter   = ""
    extra_filter  = ""
    category_filter = f"AND Category = '{category}'"
    fallback_used = None
    historical_pids = []

    if mode == 'annual':
        tournament = (master_names[0] if master_names
                      else name.split('-')[0].strip())
        base_tournament = re.sub(
            r'\b(Golf|Tennis|Racing|NASCAR|F1)\b', '',
            tournament, flags=re.IGNORECASE
        ).strip()

        if golf_round == 'weekend':
            extra_filter = """AND (
                LOWER(Name) LIKE '%saturday%' OR
                LOWER(Name) LIKE '%sunday%' OR
                LOWER(Name) LIKE '%final round%' OR
                LOWER(Name) LIKE '%third round%'
            )"""
        elif golf_round == 'weekday':
            extra_filter = """AND (
                LOWER(Name) LIKE '%thursday%' OR
                LOWER(Name) LIKE '%friday%' OR
                LOWER(Name) LIKE '%first round%' OR
                LOWER(Name) LIKE '%second round%'
            )"""

        name_filter = (
            f"AND LOWER(Name) LIKE '%{base_tournament.lower()}%'"
        )
        extra_filter += f"\nAND Report_Date < '{TODAY}'"

        scoring = f"""
            CASE WHEN LOWER(Name) LIKE '%{base_tournament.lower()}%'
                 THEN 5 ELSE 0 END +
            CASE WHEN LOWER(Name) LIKE '%friday%'
                  AND '{golf_round}' = 'weekday' THEN 2 ELSE 0 END +
            CASE WHEN LOWER(Name) LIKE '%thursday%'
                  AND '{golf_round}' = 'weekday' THEN 1 ELSE 0 END +
            CASE WHEN LOWER(Name) LIKE '%sunday%'
                  AND '{golf_round}' = 'weekend' THEN 2 ELSE 0 END +
            CASE WHEN LOWER(Name) LIKE '%saturday%'
                  AND '{golf_round}' = 'weekend' THEN 1 ELSE 0 END +
            CASE WHEN avg_order_size BETWEEN {price_anchor*0.3}
                                         AND {price_anchor*4.0}
                 THEN 3 ELSE 0 END +
            CASE WHEN total_orders > 100 THEN 2 ELSE 0 END +
            CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 2 ELSE 0 END +
            CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
        """

    elif mode == 'sports':
        if tour_tier in ['championship', 'semifinal']:
            tier_kws  = TOURNAMENT_TIERS.get(tour_tier, [])
            kw_cases  = "\n+".join([
                f"CASE WHEN LOWER(Name) LIKE '%{kw.lower()}%' THEN 5 ELSE 0 END"
                for kw in tier_kws
            ])
            kw_conds  = " OR ".join([
                f"LOWER(Name) LIKE '%{kw.lower()}%'"
                for kw in tier_kws
            ])
            extra_filter = f"AND ({kw_conds})"
            scoring = f"""
                {kw_cases} +
                CASE WHEN Category = '{category}' THEN 3 ELSE 0 END +
                CASE WHEN avg_order_size BETWEEN {price_anchor*0.25}
                                             AND {price_anchor*5.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN total_orders > 300 THEN 2 ELSE 0 END +
                CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END +
                CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
            """

        elif sport_sub in ['quarterfinal', 'tournament_early']:
            tier_kws = TOURNAMENT_TIERS.get(sport_sub, [])
            kw_cases = "\n+".join([
                f"CASE WHEN LOWER(Name) LIKE '%{kw.lower()}%' THEN 3 ELSE 0 END"
                for kw in tier_kws
            ]) if tier_kws else "0"
            scoring = f"""
                {kw_cases} +
                CASE WHEN Category = '{category}' THEN 2 ELSE 0 END +
                CASE WHEN avg_order_size BETWEEN {price_anchor*0.3}
                                             AND {price_anchor*4.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN total_orders > 100 THEN 2 ELSE 0 END +
                CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
            """

        else:
            home_team  = master_names[0] if master_names else ""
            away_team  = away_names[0]   if away_names   else ""
            home_word  = home_team.split()[0] if home_team else ""

            home_case = (
                f"CASE WHEN LOWER(Name) LIKE '%{home_team.lower()}%' THEN 4 ELSE 0 END"
                if home_team else "0"
            )
            away_case = (
                f"CASE WHEN LOWER(Name) LIKE '%{away_team.lower()}%' THEN 2 ELSE 0 END"
                if away_team else "0"
            )

            if home_word:
                name_filter = (
                    f"AND LOWER(Name) LIKE '%{home_word.lower()}%'"
                )

            scoring = f"""
                {home_case} +
                {away_case} +
                CASE WHEN City = '{p['City']}' THEN 2 ELSE 0 END +
                CASE WHEN avg_order_size BETWEEN {price_anchor*0.4}
                                             AND {price_anchor*2.5}
                     THEN 3 ELSE 0 END +
                CASE WHEN avg_order_size < {price_anchor*0.25}
                     THEN -3 ELSE 0 END +
                CASE WHEN total_quantity BETWEEN {listing_low}
                                             AND {listing_high*1.5}
                     THEN 1 ELSE 0 END +
                CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END
            """

    elif mode == 'concert':
        historical_pids = get_performer_event_ids_from_daily(master_names)

        if historical_pids:
            pids_str        = ", ".join(str(i) for i in historical_pids)
            name_filter     = f"AND PID IN ({pids_str})"
            category_filter = ""
            scoring = f"""
                CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                             AND {listing_high*3.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN avg_order_size BETWEEN {price_anchor*0.3}
                                             AND {price_anchor*4.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN total_orders > 500 THEN 3 ELSE 0 END +
                CASE WHEN total_orders > 200 THEN 2 ELSE 0 END +
                CASE WHEN total_orders > 50  THEN 1 ELSE 0 END +
                CASE WHEN City = '{p['City']}' THEN 2 ELSE 0 END +
                CASE WHEN Venue = '{p['Venue_Name'].replace(chr(39), chr(39)*2)}'
                     THEN 3 ELSE 0 END +
                CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 2 ELSE 0 END
            """
        else:
            print("  → No performer history — falling back to name + genre")
            artist_cases = "\n+".join([
                f"CASE WHEN LOWER(Name) LIKE "
                f"'%{n.lower().replace(chr(39), chr(39)*2)}%' THEN 5 ELSE 0 END"
                for n in master_names
            ]) if master_names else "0"
            scoring = f"""
                {artist_cases} +
                CASE WHEN avg_order_size BETWEEN {price_anchor*0.3}
                                             AND {price_anchor*4.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                             AND {listing_high*3.0}
                     THEN 2 ELSE 0 END +
                CASE WHEN total_orders > 200 THEN 2 ELSE 0 END +
                CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END
            """

    elif mode == 'theater':
        show_name = (master_names[0] if master_names
                     else name.split('-')[0].strip())
        show_name_clean = re.sub(
            r' in Concert| Live| - The Musical| The Musical',
            '', show_name, flags=re.IGNORECASE
        ).strip()
        name_filter = (
            f"AND LOWER(Name) LIKE "
            f"'%{show_name_clean.lower().replace(chr(39), chr(39)*2)}%'"
        )
        scoring = f"""
            CASE WHEN avg_order_size BETWEEN {price_anchor*0.3}
                                         AND {price_anchor*4.0}
                 THEN 4 ELSE 0 END +
            CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                         AND {listing_high*3.0}
                 THEN 2 ELSE 0 END +
            CASE WHEN total_orders > 50  THEN 3 ELSE 0 END +
            CASE WHEN total_orders > 20  THEN 1 ELSE 0 END +
            CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 2 ELSE 0 END +
            CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
        """

    else:
        scoring = f"""
            CASE WHEN avg_order_size BETWEEN {price_anchor*0.4}
                                         AND {price_anchor*3.0}
                 THEN 3 ELSE 0 END +
            CASE WHEN total_quantity BETWEEN {listing_low}
                                         AND {listing_high}
                 THEN 2 ELSE 0 END +
            CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END
        """

    # ── Execute comp query ───────────────────────────────────────── #
    use_cat = (category_filter
               if mode != 'concert' or not historical_pids
               else "")

    comps_query = f"""
    WITH raw AS (
        SELECT
            PID,
            Name,
            Date                AS EventDate,
            Venue, City, State, Category,
            SUM(Revenue)        AS total_revenue,
            SUM(Orders)         AS total_orders,
            SUM(Quantity)       AS total_quantity,
            AVG(Avg_Order_Size) AS avg_order_size,
            MIN(Report_Date)    AS first_report,
            MAX(Report_Date)    AS last_report
        FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
        WHERE PID != {event_id}
          AND Report_Date BETWEEN '2021-01-01' AND '{TODAY}'
          {excl_sql}
          {name_filter}
          {use_cat}
          {extra_filter}
        GROUP BY PID, Name, Date, Venue, City, State, Category
        HAVING SUM(Orders) >= {min_orders}
    ),
    scored AS (
        SELECT
            PID                 AS EventID,
            Name, EventDate, Venue, City, State, Category,
            total_revenue, total_orders, total_quantity,
            avg_order_size, first_report, last_report,
            ({scoring})         AS similarity_score
        FROM raw
    ),
    deduped AS (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY EventID
                ORDER BY similarity_score DESC, total_orders DESC
            ) AS rn
        FROM scored
    )
    SELECT
        EventID, Name, EventDate, Venue, City, State, Category,
        total_revenue, total_orders, total_quantity,
        avg_order_size, first_report, last_report, similarity_score
    FROM deduped
    WHERE rn = 1
    ORDER BY similarity_score DESC, total_orders DESC
    LIMIT {n_comps}
    """

    comps = client.query(comps_query).to_dataframe()

    # Filter future concert events
    if mode == 'concert' and not comps.empty:
        comps = comps[
            comps['EventDate'].apply(
                lambda d: (parse_event_date(str(d)) or datetime.min)
                          < datetime.now()
            )
        ].reset_index(drop=True)

    # Filter future annual events
    if mode == 'annual' and not comps.empty:
        comps = comps[
            comps['EventDate'].apply(
                lambda d: (parse_event_date(str(d)) or datetime.min)
                          < datetime.now()
            )
        ].reset_index(drop=True)

    # Theater fallback
    if mode == 'theater' and len(comps) < 5:
        print(f"  → Only {len(comps)} comps — trying fallback tiers")
        comps, fallback_used = theater_fallback_comps(
            p, show_name_clean, price_anchor,
            listing_low, listing_high,
            excl_sql, min_orders, n_comps
        )

    confidence, conf_notes = evaluate_confidence(
        comps, venue_tier, mode, listings
    )

    print(f"\nTOP {len(comps)} COMPS "
          f"[{mode.upper()} | {venue_tier.upper()}"
          f"{' | fallback: ' + fallback_used if fallback_used else ''}]:")

    if not comps.empty:
        print(comps[['EventID', 'Name', 'EventDate', 'City',
                      'total_orders', 'avg_order_size',
                      'similarity_score']].to_string())
    else:
        print("  No comps found.")

    print(f"\n🎯 Confidence: {confidence}")
    print(f"   {conf_notes}")

    comps.to_csv(f"data/comps_{event_id}.csv", index=False)

    return comps, confidence


# ================================================================== #
#  STEP 5: TRAJECTORY BUILDER                                          #
# ================================================================== #
def pull_daily_trajectories(comp_event_ids: list) -> pd.DataFrame:
    if not comp_event_ids:
        return pd.DataFrame()

    ids_str = ", ".join(str(i) for i in comp_event_ids)

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

def compute_sellthrough_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute sell-through velocity and cumulative metrics per comp event.
    Uses Orders and days_before_event from 75_Event_Daily.
    
    Returns per-event summary with:
      - total_orders: lifetime orders
      - peak_velocity_day: when daily orders peaked
      - early_sell_pct: % of orders sold 60+ days before event
      - late_sell_pct: % of orders sold in final 14 days
      - velocity_trend: ACCELERATING / STABLE / DECELERATING
    """
    if df.empty:
        return pd.DataFrame()

    results = []
    for event_id, group in df.groupby('EventID'):
        g = group.copy().sort_values('days_before_event', ascending=False)

        total_orders = g['Orders'].sum()
        if total_orders == 0:
            continue

        # Segment orders by time window
        early   = g[g['days_before_event'] >= 60]['Orders'].sum()
        mid     = g[(g['days_before_event'] >= 14) &
                    (g['days_before_event'] < 60)]['Orders'].sum()
        late    = g[g['days_before_event'] < 14]['Orders'].sum()

        early_pct = round(early / total_orders * 100, 1)
        mid_pct   = round(mid   / total_orders * 100, 1)
        late_pct  = round(late  / total_orders * 100, 1)

        # Peak velocity day
        peak_row      = g.loc[g['Orders'].idxmax()]
        peak_vel_day  = int(peak_row['days_before_event'])
        peak_vel_orders = int(peak_row['Orders'])

        # Velocity trend: compare first-half vs second-half of selling window
        max_day = g['days_before_event'].max()
        mid_day = max_day / 2
        first_half  = g[g['days_before_event'] >= mid_day]['Orders'].mean()
        second_half = g[g['days_before_event'] <  mid_day]['Orders'].mean()

        # Handle missing values safely
        if pd.isna(first_half) or pd.isna(second_half) or first_half == 0:
            trend = 'STABLE'
        elif second_half > first_half * 1.2:
            trend = 'ACCELERATING'
        elif second_half < first_half * 0.8:
            trend = 'DECELERATING'
        else:
            trend = 'STABLE'

        results.append({
            'EventID':            event_id,
            'total_orders':       int(total_orders),
            'early_sell_pct':     early_pct,
            'mid_sell_pct':       mid_pct,
            'late_sell_pct':      late_pct,
            'peak_velocity_day':  peak_vel_day,
            'peak_velocity_orders': peak_vel_orders,
            'velocity_trend':     trend,
        })

    return pd.DataFrame(results)


def aggregate_sellthrough(st_df: pd.DataFrame) -> dict:
    """
    Aggregate sell-through metrics across all comp events
    to produce a market-level sell-through profile.
    """
    if st_df.empty:
        return {}

    return {
        'median_early_pct':   round(st_df['early_sell_pct'].median(), 1),
        'median_mid_pct':     round(st_df['mid_sell_pct'].median(), 1),
        'median_late_pct':    round(st_df['late_sell_pct'].median(), 1),
        'typical_peak_day':   round(st_df['peak_velocity_day'].median(), 0),
        'pct_accelerating':   round(
            (st_df['velocity_trend'] == 'ACCELERATING').mean() * 100, 1),
        'pct_decelerating':   round(
            (st_df['velocity_trend'] == 'DECELERATING').mean() * 100, 1),
        'n_events':           len(st_df),
    }

def compute_demand_signal(
    curves: pd.DataFrame,
    days_before_event: int,
    st_summary: dict,
) -> dict:
    """
    Compute a demand signal at current days_before_event.
    Combines:
      - Orders velocity at this time horizon vs comp baseline
      - Sell-through profile (early vs late demand pattern)
      - Trajectory direction (appreciating vs decaying)

    Returns:
      signal:      HIGH / MEDIUM / LOW
      explanation: human-readable reasoning
    """
    if curves.empty or not st_summary:
        return {'signal': 'UNKNOWN', 'explanation': 'Insufficient comp data.'}

    # Get median orders at current time bucket
    bucket = (days_before_event // 7) * 7
    nearby = curves[abs(curves['day_bucket'] - bucket) <= 14]

    if nearby.empty:
        return {'signal': 'UNKNOWN', 'explanation': 'No comp data at this horizon.'}

    current_orders = float(nearby['median_orders'].median())

    # Get median orders across all time buckets for comparison
    overall_median = float(curves['median_orders'].median())

    # Velocity ratio: are we above or below typical order rate?
    velocity_ratio = current_orders / overall_median if overall_median > 0 else 1.0

    # Sell-through pattern: is demand typically early or late for this event type?
    early_pct = st_summary.get('median_early_pct', 33)
    late_pct  = st_summary.get('median_late_pct',  33)
    peak_day  = st_summary.get('typical_peak_day', 30)

    # Are we before or after the typical peak velocity day?
    past_peak = days_before_event < peak_day

    # Acceleration: are comps showing more orders as event approaches?
    pct_accel = st_summary.get('pct_accelerating', 0)
    pct_decel = st_summary.get('pct_decelerating', 0)

    # Score demand
    score = 0

    if velocity_ratio >= 1.3:
        score += 3
    elif velocity_ratio >= 0.8:
        score += 1
    else:
        score -= 1

    if pct_accel >= 50:
        score += 2
    elif pct_decel >= 50:
        score -= 2

    if early_pct >= 50 and days_before_event >= 60:
        score += 2   # Early demand type, still in prime window
    elif late_pct >= 50 and days_before_event <= 30:
        score += 2   # Late demand type, entering prime window
    elif late_pct >= 50 and days_before_event >= 60:
        score -= 1   # Late demand type, too early

    if past_peak:
        score -= 1

    # Classify
    if score >= 4:
        signal = 'HIGH'
        explanation = (
            f"Strong demand — orders at this horizon are "
            f"{velocity_ratio:.1f}x the comp average. "
            f"{pct_accel:.0f}% of comps showed accelerating sales. "
            f"Typical peak demand is at {peak_day:.0f} days before event."
        )
    elif score >= 1:
        signal = 'MEDIUM'
        explanation = (
            f"Moderate demand — orders running at "
            f"{velocity_ratio:.1f}x comp average. "
            f"Comps typically sell {early_pct:.0f}% of tickets early "
            f"and {late_pct:.0f}% in the final 2 weeks."
        )
    else:
        signal = 'LOW'
        explanation = (
            f"Weak demand signal — orders at {velocity_ratio:.1f}x comp average. "
            f"{pct_decel:.0f}% of comps showed decelerating sales at this horizon. "
            f"{'Past typical peak demand day.' if past_peak else 'Not yet at peak demand window.'}"
        )

    return {
        'signal':        signal,
        'score':         score,
        'velocity_ratio': round(velocity_ratio, 2),
        'explanation':   explanation,
    }

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
    df['day_bucket'] = (
        (df['days_before_event'] // bucket_size) * bucket_size
    )

    curves = (
        df.groupby('day_bucket')
          .agg(
              n_events           = ('EventID',        'nunique'),
              median_price_index = ('price_index',    'median'),
              p25_price_index    = ('price_index',
                                    lambda x: x.quantile(0.25)),
              p75_price_index    = ('price_index',
                                    lambda x: x.quantile(0.75)),
              median_price       = ('Avg_Order_Size', 'median'),
              median_orders      = ('Orders',         'median'),
          )
          .reset_index()
          .sort_values('day_bucket', ascending=False)
    )

    return curves[curves['n_events'] >= 2].copy()


# ================================================================== #
#  STEP 6: PREDICTION & RECOMMENDATION                                 #
# ================================================================== #
def fit_trajectory(curves: pd.DataFrame) -> pd.DataFrame:
    reliable = curves[curves['n_events'] >= 2].sort_values('day_bucket')
    if len(reliable) < 3:
        return pd.DataFrame()

    day_range = range(0, int(reliable['day_bucket'].max()) + 1)
    interp    = pd.DataFrame({'day': day_range})

    for col, src in [
        ('price_index', 'median_price_index'),
        ('p25_index',   'p25_price_index'),
        ('p75_index',   'p75_price_index'),
    ]:
        interp[col] = np.interp(
            interp['day'], reliable['day_bucket'], reliable[src]
        )

    interp['n_events'] = np.interp(
        interp['day'], reliable['day_bucket'], reliable['n_events']
    ).astype(int)

    interp['price_index_smooth'] = (
        interp['price_index']
          .rolling(window=7, center=True, min_periods=1)
          .median()
    )

    return interp.sort_values(
        'day', ascending=False
    ).reset_index(drop=True)


def get_nearest_row(traj: pd.DataFrame, day: int) -> pd.Series:
    t = traj.copy()
    t['dist'] = abs(t['day'] - day)
    return t.nsmallest(1, 'dist').iloc[0]


def generate_recommendation(
    curves: pd.DataFrame,
    current_price: float,
    days_before_event: int,
    prior_price: float = None,
    cost_basis: float = None,
    mode: str = None,
) -> dict:

    traj = fit_trajectory(curves)
    if traj.empty:
        return {
            'recommendation': 'INSUFFICIENT_DATA',
            'reasoning': 'Not enough historical comp data.',
            'predictions': {}
        }
    
    # Sports fans routinely buy within the final week — a 30-40% day-of
    # price drop is normal demand behavior, not a distress signal.
    # Use a looser threshold for sports to avoid false SELL_NOW triggers.
    is_sports = mode in ('sports',) if mode else False
    day0_decay_threshold = -30 if is_sports else -20

    current_row   = get_nearest_row(traj, days_before_event)
    current_index = float(current_row['price_index_smooth'])
    baseline      = (current_price / current_index
                     if current_index > 0 else current_price)

    future = traj[traj['day'] < days_before_event].copy()
    future['abs_price'] = future['price_index_smooth'] * baseline

    if future.empty:
        peak_price, peak_day = current_price, days_before_event
        trough_price         = current_price
    else:
        peak_row     = future.loc[future['abs_price'].idxmax()]
        peak_price   = float(peak_row['abs_price'])
        peak_day     = int(peak_row['day'])
        trough_row   = future.loc[future['abs_price'].idxmin()]
        trough_price = float(trough_row['abs_price'])

    day0_price = float(get_nearest_row(traj, 0)['price_index_smooth']) \
                 * baseline

    upside_pct   = ((peak_price   - current_price) / current_price) * 100
    downside_pct = ((trough_price - current_price) / current_price) * 100
    day0_pct     = ((day0_price   - current_price) / current_price) * 100

    # Realized decline since the prior checkpoint.
    # The model recalibrates its baseline from current price each call, so
    # it can't see a sustained decline on its own — prior_price provides
    # that anchor.
    realized_decline_pct = 0.0
    if prior_price and prior_price > 0:
        realized_decline_pct = (
            (current_price - prior_price) / prior_price * 100
        )

    avg_band     = float(
        (curves['p75_price_index'] - curves['p25_price_index']).mean()
    )
    uncertainty  = (" ⚠️ High price variability — treat as directional only."
                    if avg_band > 1.2 else "")
    max_support  = int(curves['n_events'].max())
    support_note = f"Supported by {max_support} historical comp events."

    # Price predictions
    predictions = {}
    for target_day in [90, 60, 30, 14, 7, 0]:
        if target_day >= days_before_event:
            continue
        row        = get_nearest_row(traj, target_day)
        pred_price = baseline * float(row['price_index_smooth'])
        change_pct = ((pred_price - current_price) / current_price) * 100
        predictions[target_day] = {
            'predicted_price': round(pred_price, 2),
            'low_estimate':    round(
                baseline * float(row['p25_index']), 2),
            'high_estimate':   round(
                baseline * float(row['p75_index']), 2),
            'change_vs_now':   round(change_pct, 1),
            'n_events':        int(row['n_events']),
        }

    # P&L
    pnl = {}
    if cost_basis:
        pnl = {
            'cost_basis':          round(cost_basis, 2),
            'current_profit_pct':  round(((current_price - cost_basis) / cost_basis) * 100, 1),
            'current_profit_abs':  round(current_price - cost_basis, 2),
            'peak_profit_pct':     round(((peak_price - cost_basis) / cost_basis) * 100, 1),
            'peak_profit_abs':     round(peak_price - cost_basis, 2),
            'day0_profit_pct':     round(((day0_price - cost_basis) / cost_basis) * 100, 1),
            'day0_profit_abs':     round(day0_price - cost_basis, 2),
        }

    # ── Decision logic ──────────────────────────────────────────────── #

    # [1] Realized decline stop-loss — fires before any HOLD check.
    # The tautological baseline (baseline = current_price / current_index)
    # means the model re-anchors at every checkpoint and can keep seeing
    # "upside" even as prices spiral down. prior_price breaks that loop.
    # Requires BOTH:
    #   a) actual price has dropped ≥20% from the day-90 anchor, AND
    #   b) the comp trajectory also predicts a bad endpoint (day0_pct < -10)
    # The second condition filters out temporary dips where the trajectory
    # still correctly predicts recovery — in those cases the decline is noise,
    # not a signal. Without it, V-shaped events get sold at the trough.
    if realized_decline_pct <= -20 and day0_pct < -10 and upside_pct < 30 \
            and max_support >= 5:
        rec = 'SELL_NOW'
        reasoning = (
            f"Price has dropped {realized_decline_pct:.1f}% since last "
            f"evaluation (${prior_price:.0f} → ${current_price:.0f}). "
            f"Actual trajectory is underperforming comp predictions — "
            f"sell now rather than wait for further decay. "
            f"{support_note}{uncertainty}"
        )
        return {
            'recommendation':      rec,
            'reasoning':           reasoning,
            'current_price':       round(current_price, 2),
            'baseline_price':      round(baseline, 2),
            'days_remaining':      days_before_event,
            'peak_price':          round(peak_price, 2),
            'peak_day':            peak_day,
            'trough_price':        round(trough_price, 2),
            'day0_price':          round(day0_price, 2),
            'upside_pct':          round(upside_pct, 1),
            'downside_pct':        round(downside_pct, 1),
            'day0_pct':            round(day0_pct, 1),
            'realized_decline_pct': round(realized_decline_pct, 1),
            'predictions':         predictions,
            **pnl,
        }

    # [2] Late-window override — within 14 days default to SELL_NOW.
    # MONITOR at day 14 with no forced sell = silent day-0 sell at a
    # typically lower price. Only hold if there's strong spike evidence.
    if days_before_event <= 14:
        if upside_pct >= 20 and day0_pct > -5 and max_support >= 5:
            rec = 'HOLD'
            reasoning = (
                f"Strong late-window spike — comps show "
                f"{upside_pct:.1f}% appreciation to ${peak_price:.0f} "
                f"near event day. {support_note}"
            )
        else:
            rec = 'SELL_NOW'
            reasoning = (
                f"Within 14 days — sell now. Comps suggest "
                f"~${day0_price:.0f} at event day "
                f"({day0_pct:.1f}% vs current ${current_price:.0f}). "
                f"Holding this late risks illiquidity and further decay. "
                f"{support_note}{uncertainty}"
            )
        return {
            'recommendation':      rec,
            'reasoning':           reasoning,
            'current_price':       round(current_price, 2),
            'baseline_price':      round(baseline, 2),
            'days_remaining':      days_before_event,
            'peak_price':          round(peak_price, 2),
            'peak_day':            peak_day,
            'trough_price':        round(trough_price, 2),
            'day0_price':          round(day0_price, 2),
            'upside_pct':          round(upside_pct, 1),
            'downside_pct':        round(downside_pct, 1),
            'day0_pct':            round(day0_pct, 1),
            'realized_decline_pct': round(realized_decline_pct, 1),
            'predictions':         predictions,
            **pnl,
        }

    # Decision variables (days > 14)
    risk_reward  = abs(downside_pct) / max(upside_pct, 0.1)
    # Loosened from ratio > 3.0 / downside < -20 — previous thresholds
    # were too strict and let many deteriorating events slip through.
    poor_rr      = risk_reward > 2.0 and downside_pct < -15
    hold_days    = days_before_event - peak_day

    timing_note  = (
        f" Note: Peak window is only {hold_days} days away — act quickly."
        if hold_days <= 21 else ""
    )

    if poor_rr:
        rec = 'SELL_NOW'
        reasoning = (
            f"Unfavorable risk/reward — upside of "
            f"+{max(upside_pct, 0):.1f}% does not justify downside of "
            f"{downside_pct:.1f}%. Comps show price decay toward "
            f"${day0_price:.0f} at event day. "
            f"{support_note}{uncertainty}"
        )
    elif upside_pct >= 10 and peak_day > 7:
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
    elif day0_pct < day0_decay_threshold:
        # Severe endpoint decay overrides any mid-trajectory peak.
        rec = 'SELL_NOW'
        reasoning = (
            f"Comps show severe price decay to ~${day0_price:.0f} by "
            f"event day ({day0_pct:.1f}% vs now). "
            f"Mid-trajectory peak of ${peak_price:.0f} doesn't compensate "
            f"for the risk of missing it. {support_note}{uncertainty}"
        )
    elif downside_pct < -15 and upside_pct < 5 and day0_pct < -10:
        rec = 'SELL_NOW'
        reasoning = (
            f"Comps show consistent decay — expected range "
            f"${trough_price:.0f}–${peak_price:.0f}, "
            f"day-of ~${day0_price:.0f} ({day0_pct:.1f}% vs now). "
            f"{support_note}{uncertainty}"
        )
    elif abs(upside_pct) < 5 and abs(downside_pct) < 10:
        if realized_decline_pct <= -12:
            rec = 'SELL_NOW'
            reasoning = (
                f"Comp trajectory appears flat but actual price has "
                f"declined {realized_decline_pct:.1f}% since day-90. "
                f"Selling into observed weakness rather than waiting. "
                f"{support_note}{uncertainty}"
            )
        else:
            rec = 'MONITOR'
            reasoning = (
                f"Flat trajectory. Upside {upside_pct:.1f}%, "
                f"downside {downside_pct:.1f}%. No urgency. "
                f"Re-evaluate in 14 days. {support_note}{uncertainty}"
            )
    elif day0_pct < -15 and upside_pct < 15:
        rec = 'SELL_NOW'
        reasoning = (
            f"Comps show price decays to ~${day0_price:.0f} by event day "
            f"({day0_pct:.1f}%). Limited upside doesn't justify the risk. "
            f"{support_note}{uncertainty}"
        )
    else:
        if realized_decline_pct <= -12:
            rec = 'SELL_NOW'
            reasoning = (
                f"Mixed comp signals but actual price has declined "
                f"{realized_decline_pct:.1f}% since day-90. "
                f"Observed price path takes precedence. "
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
        'recommendation':       rec,
        'reasoning':            reasoning,
        'current_price':        round(current_price, 2),
        'baseline_price':       round(baseline, 2),
        'days_remaining':       days_before_event,
        'peak_price':           round(peak_price, 2),
        'peak_day':             peak_day,
        'trough_price':         round(trough_price, 2),
        'day0_price':           round(day0_price, 2),
        'upside_pct':           round(upside_pct, 1),
        'downside_pct':         round(downside_pct, 1),
        'day0_pct':             round(day0_pct, 1),
        'realized_decline_pct': round(realized_decline_pct, 1),
        'predictions':          predictions,
        **pnl,
    }


# ================================================================== #
#  STEP 7: PRINT REPORT                                                #
# ================================================================== #
def print_report(info: dict, comps: pd.DataFrame,
                 confidence: str, market: dict, rec: dict,
                 tc_share: dict = None,
                 demand: dict = None,
                 st_summary: dict = None,
                 decay: dict = None,
                 section: dict = None):

    print(f"\n{'='*65}")
    print(f"  TICKETCITY ANALYSIS REPORT")
    print(f"{'='*65}")
    print(f"  Event:    {info['Name']}")
    print(f"  Date:     {info['LocalDate']}")
    print(f"  Venue:    {info['Venue_Name']}, "
          f"{info['City']}, {info['State']}")
    print(f"  Category: {info['Category_Name']}")
    print(f"{'='*65}")

    print(f"\n📊 MARKET SNAPSHOT")
    print(f"  Current listing range:  "
          f"${info['MinPrice']} – ${info['MaxPrice']}")
    print(f"  Active listings:        {info['ListingCount']:,}")
    if market:
        print(f"  Most recent sale price: "
              f"${market['avg_order_size']:.2f} "
              f"({market['days_before_event']} days before event, "
              f"{market['report_date']})")
        print(f"  Orders on that date:    {market['orders']}")

    CATEGORY_RISK = {
        'Comedy':          'HIGH — comp pools unreliable, trajectory highly variable',
        'NCAA Basketball': 'HIGH — demand spikes near game day not captured by comps',
        'Rock':            'MEDIUM-HIGH — artist-specific demand not reflected in genre comps',
        'Pop':             'MEDIUM-HIGH — outlier events skew comp trajectories',
        'MLB Baseball':    'MEDIUM — flat decay curves, low comp differentiation',
    }
    cat_risk = CATEGORY_RISK.get(info['Category_Name'], 'STANDARD')
    print(f"\n⚠️  CATEGORY RISK: {cat_risk}")

    if st_summary:
        print(f"\n📉 SELL-THROUGH PATTERN (from {st_summary['n_events']} comps)")
        print(f"  Early (60+ days):  {st_summary['median_early_pct']}% of sales")
        print(f"  Mid (14-60 days):  {st_summary['median_mid_pct']}% of sales")
        print(f"  Late (0-14 days):  {st_summary['median_late_pct']}% of sales")
        print(f"  Typical peak day:  "
              f"{st_summary['typical_peak_day']:.0f} days before event")
        
    if decay:
        decay_icons = {"RELIABLE": "🟢", "MODERATE": "🟡", "HIGH_DECAY": "🔴", "UNKNOWN": "⚪"}
        flag_icons  = {
            "SELL_EARLY_REQUIRED": "🔴", "HOLD_THROUGH_PEAK": "🟢",
            "SELL_ANYTIME": "🟡",        "LAST_MINUTE_DEMAND": "🟠", "UNKNOWN": "⚪"
        }
        d_icon = decay_icons.get(decay["decay_class"], "⚪")
        f_icon = flag_icons.get(decay["timing_flag"], "⚪")
        print(f"\n{d_icon} DECAY PROFILE:  {decay['decay_class']}  "
              f"(median decay {decay.get('med_decay_pct', 0):+.1f}%  "
              f"| early sell {decay.get('med_early_pct', 0):.0f}%  "
              f"| late sell {decay.get('med_late_pct', 0):.0f}%)")
        print(f"{f_icon} TIMING FLAG:    {decay['timing_flag']}")
        print(f"   {decay['guidance']}")

        # ── Section & Market Dynamics ────────────────────────────────
    if section:
        print(f"\n🏟️ SECTION & MARKET DYNAMICS")
        print(f"  Snapshot days analyzed: {section.get('snapshot_days', 0)}")
        print(f"  Marketplace sources:    {section.get('source_count', 0)}")

        ga_tbl = section.get("ga_vs_reserved")
        if isinstance(ga_tbl, pd.DataFrame) and not ga_tbl.empty:
            print(f"\n  GA VS RESERVED")
            for _, row in ga_tbl.iterrows():
                print(
                    f"   {row['segment']:<12} | "
                    f"Listings: {int(row['n_listings']):>5} | "
                    f"Tickets: {int(row['total_tickets']):>5} | "
                    f"Median: ${float(row['median_price']):.2f}"
                )

        scarcity = section.get("scarcity_leaders")
        if isinstance(scarcity, pd.DataFrame) and not scarcity.empty:
            print(f"\n  TIGHTEST SUPPLY SECTIONS")
            for _, row in scarcity.head(3).iterrows():
                print(
                    f"   {row['section_group']} ({row['ticket_class']}) | "
                    f"Tickets: {int(row['latest_ticket_count'])} | "
                    f"Median: ${float(row['latest_median_price']):.2f} | "
                    f"Flag: {row['behavior_flag']}"
                )

        decay_tbl = section.get("decay_sections")
        if isinstance(decay_tbl, pd.DataFrame) and not decay_tbl.empty:
            print(f"\n  HIGHEST DECAY-RISK SECTIONS")
            for _, row in decay_tbl.head(3).iterrows():
                price_chg = row['price_change_14d_pct']
                print(
                    f"   {row['section_group']} ({row['ticket_class']}) | "
                    f"14d price: {price_chg:+.1f}% | "
                    f"Tickets: {int(row['latest_ticket_count'])}"
                )

        tc_tbl = section.get("tc_position_latest")
        if isinstance(tc_tbl, pd.DataFrame) and not tc_tbl.empty:
            print(f"\n  TICKETCITY SHARE BY SECTION")
            for _, row in tc_tbl.head(3).iterrows():
                print(
                    f"   {row['section_group']} ({row['ticket_class']}) | "
                    f"TC ticket share: {float(row['tc_ticket_share']):.1f}% | "
                    f"TC listing share: {float(row['tc_listing_share']):.1f}%"
                )
        
        source_tbl = section.get("source_summary")
        if isinstance(source_tbl, pd.DataFrame) and not source_tbl.empty:
            print(f"\n  MARKETPLACE BREAKDOWN")
            latest_source_date = source_tbl["snapshot_date"].max()
            latest_sources = (
                source_tbl[source_tbl["snapshot_date"] == latest_source_date]
                .sort_values("listing_count", ascending=False)
            )
            for _, row in latest_sources.head(5).iterrows():
                print(
                    f"   {str(row['DataSource']):<18} | "
                    f"Listings: {int(row['listing_count']):>5} | "
                    f"Tickets: {int(row['ticket_count']):>5} | "
                    f"Median: ${float(row['median_price']):.2f}"
                )

    print(f"\n📈 PRICE PREDICTIONS")
    print(f"  Baseline price: ${rec['baseline_price']}")
    for day, data in sorted(
        rec.get('predictions', {}).items(), reverse=True
    ):
        direction = "↑" if data['change_vs_now'] > 0 else "↓"
        print(f"  At {day:>3} days:  "
              f"${data['predicted_price']:>7.2f}  "
              f"{direction}{abs(data['change_vs_now']):.1f}%  "
              f"[${data['low_estimate']:.0f}–${data['high_estimate']:.0f}]"
              f"  ({data['n_events']} events)")

    icons = {
        'SELL_NOW':          '🔴',
        'HOLD':              '🟢',
        'MONITOR':           '🟡',
        'INSUFFICIENT_DATA': '⚪'
    }
    icon = icons.get(rec['recommendation'], '⚪')

    print(f"\n{icon} RECOMMENDATION: {rec['recommendation']}")
    print(f"  {rec['reasoning']}")

    if 'cost_basis' in rec:
        print(f"\n💰 P&L SUMMARY (per ticket)")
        print(f"  Cost basis:       ${rec['cost_basis']:.2f}")
        print(f"  Sell now:         ${rec['current_price']:.2f}  "
              f"→  {rec['current_profit_pct']:+.1f}%  "
              f"(${rec['current_profit_abs']:+.2f} profit)")
        if rec['peak_price'] > rec['current_price'] * 1.02:
            print(f"  Sell at peak:     ${rec['peak_price']:.2f}  "
                  f"→  {rec['peak_profit_pct']:+.1f}%  "
                  f"(${rec['peak_profit_abs']:+.2f} profit)  "
                  f"[{rec['peak_day']} days before event]")
        else:
            print(f"  Sell at peak:     already at or past peak —"
                  f" no meaningful upside remaining")
        print(f"  Wait till day-of: ${rec['day0_price']:.2f}  "
              f"→  {rec['day0_profit_pct']:+.1f}%  "
              f"(${rec['day0_profit_abs']:+.2f} profit)")

    peak_label = (
        "(no future upside — at or past peak)"
        if rec['peak_price'] <= rec['current_price'] * 1.02
        else f"target: sell at {rec['peak_day']} days before event"
    )
    print(f"\n  Peak window:  ${rec['peak_price']:.2f} — {peak_label}")
    print(f"  Day-of price: ${rec['day0_price']:.2f} "
          f"({rec['day0_pct']:+.1f}% vs now)")
    print(f"{'='*65}\n")

    print(f"\n🎫 YOUR POSITION")
    print(f"  Listed price:     ${rec['current_price']:.2f}")
    if market:
        print(f"  Market avg sale:  ${market['avg_order_size']:.2f} "
              f"(context only — blended across all ticket types)")
    print(f"{'='*65}\n")

def classify_section(name: str) -> str:
    """
    Normalize raw section labels into broad seat-location buckets.
    """
    if not name:
        return "Unknown"

    n = str(name).upper()

    if any(k in n for k in [
        "GA", "GENERAL ADMISSION", "STANDING", "LAWN", "FIELD", "INFIELD", "SRO"
    ]):
        return "GA / General Admission"

    if any(k in n for k in ["FLOOR", "PIT", "MOSH"]):
        return "Floor / Pit"

    if any(k in n for k in [
        "CLUB", "SUITE", "VIP", "BOX", "LOGE", "LOUNGE",
        "TERRACE", "PREMIUM", "PLATINUM"
    ]):
        return "Club / Suite / VIP"

    if any(k in n for k in [
        "UPPER", "300", "400", "500", "GRANDSTAND", "BLEACHER"
    ]):
        return "Upper Bowl"

    if any(k in n for k in [
        "LOWER", "100", "200", "MAIN", "ORCHESTRA", "MEZZANINE"
    ]):
        return "Lower Bowl"

    return "Other"


def get_inventory_snapshots(
    event_id: int,
    lookback_days: int = 45,
) -> pd.DataFrame:
    """
    Pull InventoryStream snapshots for one event over a recent window.

    This provides:
    - section-level price movement
    - listing/ticket scarcity over time
    - cross-marketplace movement by DataSource

    InventoryStream is safe for this because we always filter by exact EventID.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_unix = int(cutoff_dt.timestamp())

    query = f"""
    WITH base AS (
        SELECT
            SnapshotId,
            Time,
            TIMESTAMP_SECONDS(Time)                AS snapshot_ts,
            DATE(TIMESTAMP_SECONDS(Time))          AS snapshot_date,
            EventID,
            DataSource,
            BlockId,
            SectionName,
            Row,
            CAST(Quantity AS INT64)               AS Quantity,
            CAST(Price AS FLOAT64)                AS Price,
            StockType
        FROM `data-ticketcity.TC_Data.InventoryStream`
        WHERE EventID = {event_id}
          AND Time >= {cutoff_unix}
          AND Price > 0
          AND Price < 50000
          AND Quantity > 0
    )
    SELECT
        SnapshotId,
        Time,
        snapshot_ts,
        snapshot_date,
        EventID,
        DataSource,
        BlockId,
        SectionName,
        Row,
        Quantity,
        Price,
        StockType
    FROM base
    ORDER BY snapshot_ts ASC
    """

    job = client.query(query)
    df = job.to_dataframe()

    if df.empty:
        return pd.DataFrame()

    df["section_group"] = df["SectionName"].apply(classify_section)
    df["ticket_class"]  = df.apply(
        lambda r: classify_ticket_class(r["SectionName"], r["Row"]),
        axis=1
    )

    # Snapshot-level dedupe safeguard
    df = df.drop_duplicates(
        subset=["SnapshotId", "BlockId", "SectionName", "Row", "Price", "Quantity"]
    ).copy()

    return df


def get_ticketcity_section_position(
    event_id: int,
    lookback_days: int = 45,
) -> pd.DataFrame:
    """
    Compare TicketCity-owned inventory to total market inventory by snapshot + section.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_unix = int(cutoff_dt.timestamp())

    query = f"""
    WITH market AS (
        SELECT
            i.SnapshotId,
            DATE(TIMESTAMP_SECONDS(i.Time))            AS snapshot_date,
            TIMESTAMP_SECONDS(i.Time)                  AS snapshot_ts,
            i.BlockId,
            i.SectionName,
            i.Row,
            CAST(i.Quantity AS INT64)                 AS Quantity,
            CAST(i.Price AS FLOAT64)                  AS Price
        FROM `data-ticketcity.TC_Data.InventoryStream` i
        WHERE i.EventID = {event_id}
          AND i.Time >= {cutoff_unix}
          AND i.Price > 0
          AND i.Price < 50000
          AND i.Quantity > 0
    ),
    tc AS (
        SELECT DISTINCT
            CAST(__key__.name AS STRING)              AS BlockId
        FROM `data-ticketcity.TC_Data.TicketCity_Tickets`
        WHERE tfs_event_id = {event_id}
           OR CAST(tfs_event_id AS STRING) = '{event_id}'
    )
    SELECT
        m.SnapshotId,
        m.snapshot_date,
        m.snapshot_ts,
        m.BlockId,
        m.SectionName,
        m.Row,
        m.Quantity,
        m.Price,
        CASE WHEN tc.BlockId IS NOT NULL THEN 1 ELSE 0 END AS is_tc
    FROM market m
    LEFT JOIN tc
        ON m.BlockId = tc.BlockId
    ORDER BY m.snapshot_ts ASC
    """

    df = client.query(query).to_dataframe()
    if df.empty:
        return pd.DataFrame()

    df["section_group"] = df["SectionName"].apply(classify_section)
    df["ticket_class"] = df.apply(
        lambda r: classify_ticket_class(r["SectionName"], r["Row"]),
        axis=1
    )

    grouped = (
        df.groupby(["snapshot_date", "section_group", "ticket_class"], dropna=False)
          .agg(
              market_listing_count=("BlockId", "nunique"),
              market_ticket_count=("Quantity", "sum"),
              market_median_price=("Price", "median"),
              tc_listing_count=("is_tc", "sum"),
          )
          .reset_index()
    )

    tc_tickets = (
        df[df["is_tc"] == 1]
        .groupby(["snapshot_date", "section_group", "ticket_class"], dropna=False)["Quantity"]
        .sum()
        .reset_index(name="tc_ticket_count")
    )

    grouped = grouped.merge(
        tc_tickets,
        on=["snapshot_date", "section_group", "ticket_class"],
        how="left"
    )
    grouped["tc_ticket_count"] = grouped["tc_ticket_count"].fillna(0)

    grouped["tc_listing_share"] = np.where(
        grouped["market_listing_count"] > 0,
        grouped["tc_listing_count"] / grouped["market_listing_count"] * 100,
        np.nan
    )
    grouped["tc_ticket_share"] = np.where(
        grouped["market_ticket_count"] > 0,
        grouped["tc_ticket_count"] / grouped["market_ticket_count"] * 100,
        np.nan
    )

    return grouped.sort_values(
        ["snapshot_date", "tc_ticket_share"],
        ascending=[True, False]
    )


def compute_section_market_dynamics(snapshot_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build:
    1) point-in-time section summary (latest snapshot)
    2) section trend table across the snapshot window
    """
    if snapshot_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Use the most recent snapshot date for the current state summary
    latest_date = snapshot_df["snapshot_date"].max()
    latest = snapshot_df[snapshot_df["snapshot_date"] == latest_date].copy()

    section_summary = (
        latest.groupby(["section_group", "ticket_class"], dropna=False)
              .agg(
                  n_listings=("BlockId", "nunique"),
                  total_tickets=("Quantity", "sum"),
                  median_price=("Price", "median"),
                  min_price=("Price", "min"),
                  max_price=("Price", "max"),
              )
              .reset_index()
              .sort_values(["ticket_class", "median_price"], ascending=[True, True])
    )

    daily = (
        snapshot_df.groupby(
            ["snapshot_date", "section_group", "ticket_class"], dropna=False
        )
        .agg(
            listing_count=("BlockId", "nunique"),
            ticket_count=("Quantity", "sum"),
            median_price=("Price", "median"),
            min_price=("Price", "min"),
            max_price=("Price", "max"),
        )
        .reset_index()
        .sort_values(["section_group", "ticket_class", "snapshot_date"])
    )

    trend_rows = []

    for (section_group, ticket_class), grp in daily.groupby(
        ["section_group", "ticket_class"], dropna=False
    ):
        g = grp.sort_values("snapshot_date").copy()
        if g["snapshot_date"].nunique() < 3:
            continue

        if g["listing_count"].max() < 2:
            continue
        first = g.iloc[0]
        last  = g.iloc[-1]

        # 14-day anchor: closest row at or before last_date - 14d
        anchor_date = last["snapshot_date"] - pd.Timedelta(days=14)
        anchor_pool = g[g["snapshot_date"] <= anchor_date]
        anchor = anchor_pool.iloc[-1] if not anchor_pool.empty else first

        price_change_14d   = pct_change_safe(anchor["median_price"], last["median_price"])
        ticket_change_14d  = pct_change_safe(anchor["ticket_count"], last["ticket_count"])
        listing_change_14d = pct_change_safe(anchor["listing_count"], last["listing_count"])

        scarcity_index = (
            1000 / last["ticket_count"] if last["ticket_count"] and last["ticket_count"] > 0 else np.nan
        )

        behavior = classify_market_behavior(
            price_change_14d=price_change_14d,
            ticket_change_14d=ticket_change_14d,
            listing_change_14d=listing_change_14d,
        )

        trend_rows.append({
            "section_group": section_group,
            "ticket_class": ticket_class,
            "days_observed": int((last["snapshot_date"] - first["snapshot_date"]).days),
            "latest_listing_count": int(last["listing_count"]),
            "latest_ticket_count": int(last["ticket_count"]),
            "latest_median_price": float(last["median_price"]),
            "price_change_14d_pct": price_change_14d,
            "ticket_change_14d_pct": ticket_change_14d,
            "listing_change_14d_pct": listing_change_14d,
            "scarcity_index": scarcity_index,
            "behavior_flag": behavior,
        })

    trends = pd.DataFrame(trend_rows)

    if not trends.empty:
        trends = trends.sort_values(
            ["behavior_flag", "scarcity_index", "latest_median_price"],
            ascending=[True, False, False]
        )

    return section_summary, trends


def build_ga_vs_reserved(section_summary: pd.DataFrame) -> pd.DataFrame:
    """
    Roll section summary into GA vs RESERVED grouping.
    """
    if section_summary.empty:
        return pd.DataFrame()

    df = section_summary.copy()
    df["segment"] = np.where(df["ticket_class"] == "GA", "GA / Floor", "Reserved")

    out = (
        df.groupby("segment", dropna=False)
          .agg(
              n_listings=("n_listings", "sum"),
              total_tickets=("total_tickets", "sum"),
              median_price=("median_price", "median"),
              min_price=("min_price", "min"),
              max_price=("max_price", "max"),
          )
          .reset_index()
          .sort_values("median_price")
    )
    return out


def run_section_analysis(event_id: int, lookback_days: int = 45) -> dict:
    """
    Full Deliverable 1 section + marketplace dynamics analysis.

    Returns:
    - latest section summary
    - GA vs reserved rollup
    - section trend / scarcity / decay labels
    - TicketCity share by section
    """

    print(f"\n⏳ Running section-level market dynamics analysis.")

    snapshots = get_inventory_snapshots(event_id, lookback_days=lookback_days)
    if snapshots.empty:
        print(f"   No InventoryStream data for EventID {event_id}")
        print(f"   (InventoryStream only holds recent history and active events)")
        return {}

    n_snapshots = snapshots["snapshot_date"].nunique()
    n_sources   = snapshots["DataSource"].nunique() if "DataSource" in snapshots.columns else 0
    print(f"   {len(snapshots):,} rows across {n_snapshots} snapshot days | {n_sources} sources")

    section_summary, section_trends = compute_section_market_dynamics(snapshots)
    ga_vs_reserved = build_ga_vs_reserved(section_summary)
    tc_position = get_ticketcity_section_position(event_id, lookback_days=lookback_days)
    source_summary = (
    snapshots.groupby(["snapshot_date", "DataSource"], dropna=False)
    .agg(
        listing_count=("BlockId", "nunique"),
        ticket_count=("Quantity", "sum"),
        median_price=("Price", "median"),
    )
    .reset_index()
)

    scarcity_leaders = pd.DataFrame()
    decay_sections   = pd.DataFrame()
    reliable_sections = pd.DataFrame()

    

    if not section_trends.empty:
        scarcity_leaders = section_trends.sort_values(
            ["scarcity_index", "latest_median_price"],
            ascending=[False, False]
        ).head(5)

        decay_sections = section_trends[
            section_trends["behavior_flag"] == "DECAY_RISK"
        ].sort_values("price_change_14d_pct")

        reliable_sections = section_trends[
            section_trends["behavior_flag"] == "RELIABLE"
        ].sort_values("ticket_change_14d_pct")

    tc_latest = pd.DataFrame()
    if not tc_position.empty:
        latest_tc_date = tc_position["snapshot_date"].max()
        tc_latest = (
            tc_position[tc_position["snapshot_date"] == latest_tc_date]
            .sort_values("tc_ticket_share", ascending=False)
        )

    return {
        "total_rows": int(len(snapshots)),
        "snapshot_days": int(n_snapshots),
        "source_count": int(n_sources),
        "total_listings": int(section_summary["n_listings"].sum()) if not section_summary.empty else 0,
        "section_summary": section_summary,
        "ga_vs_reserved": ga_vs_reserved,
        "section_trends": section_trends,
        "scarcity_leaders": scarcity_leaders,
        "decay_sections": decay_sections,
        "reliable_sections": reliable_sections,
        "tc_position_latest": tc_latest,
        "source_summary": source_summary,
    }

def classify_decay_risk(comp_ids: list) -> dict:
    """
    Classify the comp pool's typical decay profile.
    Returns: decay_class (RELIABLE/MODERATE/HIGH_DECAY),
             timing_flag, and a guidance string.
    """
    if not comp_ids:
        return {"decay_class": "UNKNOWN", "timing_flag": "UNKNOWN", "guidance": "No comp data."}

    ids_str = ", ".join(str(i) for i in comp_ids)
    query = f"""
    SELECT
        PID AS event_id,
        Avg_Order_Size AS price,
        Orders,
        DATE_DIFF(
            SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8)),
            Report_Date, DAY
        ) AS days_before_event
    FROM `data-ticketcity.TFS_Reports.75_Event_Daily`
    WHERE PID IN ({ids_str})
      AND Orders > 0 AND Avg_Order_Size > 0
    ORDER BY PID, Report_Date ASC
    """
    df = client.query(query).to_dataframe()
    df = df[(df["days_before_event"] >= 0) & (df["days_before_event"] <= 365)]

    if df.empty:
        return {"decay_class": "UNKNOWN", "timing_flag": "UNKNOWN", "guidance": "No trajectory data."}

    decay_pcts, early_pcts, late_pcts, peak_uplifts = [], [], [], []

    for eid, grp in df.groupby("event_id"):
        g = grp.sort_values("days_before_event", ascending=False)
        baseline_w = g[(g["days_before_event"] >= 60) & (g["days_before_event"] <= 120)]["price"]
        baseline = baseline_w.median() if len(baseline_w) >= 2 else g["price"].iloc[0]
        if baseline <= 0:
            continue
        day0_rows = g[g["days_before_event"] <= 7]["price"]
        if day0_rows.empty:
            continue
        day0_price = float(day0_rows.median())
        total_orders = g["Orders"].sum()
        if total_orders == 0:
            continue
        early_pct = float(g[g["days_before_event"] >= 60]["Orders"].sum() / total_orders * 100)
        late_pct  = float(g[g["days_before_event"] <= 14]["Orders"].sum() / total_orders * 100)
        peak_uplift = float((g["price"].max() - baseline) / baseline * 100)
        decay_pcts.append(((day0_price - baseline) / baseline) * 100)
        early_pcts.append(early_pct)
        late_pcts.append(late_pct)
        peak_uplifts.append(peak_uplift)

    if not decay_pcts:
        return {"decay_class": "UNKNOWN", "timing_flag": "UNKNOWN", "guidance": "Insufficient data."}

    med_decay     = float(np.median(decay_pcts))
    med_early     = float(np.median(early_pcts))
    med_late      = float(np.median(late_pcts))
    med_peak      = float(np.median(peak_uplifts))

    # Classify
    if med_decay >= -10 and med_early >= 40:
        decay_class = "RELIABLE"
    elif med_decay >= -10 and med_peak >= 15:
        decay_class = "RELIABLE"
    elif med_decay <= -30:
        decay_class = "HIGH_DECAY"
    elif med_late >= 60 and med_early <= 20 and med_decay <= -15:
        decay_class = "HIGH_DECAY"
    else:
        decay_class = "MODERATE"

    # Timing flag
    if decay_class == "HIGH_DECAY" and med_late >= 50:
        flag     = "LAST_MINUTE_DEMAND"
        guidance = (
            f"High decay but demand concentrates late ({med_late:.0f}% in final 14 days). "
            f"Consider holding to ~21 days out to capture last-minute surge. "
            f"Risk: if surge doesn't materialize, price will be well below baseline."
        )
    elif decay_class == "HIGH_DECAY":
        flag     = "SELL_EARLY_REQUIRED"
        guidance = (
            f"Comps show consistent price erosion to event day (median {med_decay:.1f}%). "
            f"Sell before day 60 to capture near-baseline value."
        )
    elif decay_class == "RELIABLE" and med_peak >= 15:
        flag     = "HOLD_THROUGH_PEAK"
        guidance = (
            f"Reliable event type with a demand peak ~{med_peak:.1f}% above baseline. "
            f"Hold through the peak window then sell into strength."
        )
    elif decay_class == "RELIABLE":
        flag     = "SELL_ANYTIME"
        guidance = (
            f"Price stays within 10% of baseline through event day. "
            f"Timing is flexible — focus on listing price, not urgency."
        )
    elif med_peak >= 10:
        flag     = "HOLD_THROUGH_PEAK"
        guidance = (
            f"Moderate decay with a mid-trajectory peak ({med_peak:.1f}% above baseline). "
            f"Target selling in the peak window. Avoid holding into the final 2 weeks."
        )
    else:
        flag     = "SELL_ANYTIME"
        guidance = (
            f"Moderate, gradual decay ({med_decay:.1f}% to event day). "
            f"Sell when convenient but complete the sale before the final 14-day window."
        )

    return {
        "decay_class":  decay_class,
        "timing_flag":  flag,
        "guidance":     guidance,
        "med_decay_pct": round(med_decay, 1),
        "med_early_pct": round(med_early, 1),
        "med_late_pct":  round(med_late, 1),
        "med_peak_uplift": round(med_peak, 1),
    }
# ================================================================== #
#  MAIN PIPELINE                                                       #
# ================================================================== #
def run_pipeline(event_id: int,
                 listed_price: float = None,
                 cost_basis: float = None,
                 n_comps: int = 15) -> dict:

    print(f"\n⏳ Fetching event info...")
    info = get_event_info(event_id)
    if info is None:
        print(f"❌ EventID {event_id} not found.")
        sys.exit(1)

    print(f"   {info['Name']}")
    print(f"   {info['Category_Name']} | "
          f"{info['Venue_Name']}, {info['City']}")

    print(f"\n⏳ Fetching current market data...")
    market = get_current_market_price(event_id)

    if market:
        print(f"   Market avg recent sale: ${market['avg_order_size']:.2f}")
        print(f"   Days before event:      {market['days_before_event']}")

    if listed_price:
        current_price = listed_price
        print(f"   Your listed price:      ${current_price:.2f}")
    else:
        current_price = float(info['MinPrice'])
        print(f"   Using MinPrice as default: ${current_price:.2f}")

    # Days before event — always from actual event date
    try:
        event_dt    = datetime.fromisoformat(
            str(info['LocalDate']).replace(' ', 'T')
        )
        days_before = max(0, (event_dt - datetime.now()).days)
        print(f"   Days before event:      {days_before} (from event date)")
    except Exception:
        if market and market['days_before_event']:
            days_before = market['days_before_event']
            print(f"   Days before event:      {days_before} (from last sale)")
        else:
            days_before = 90
            print(f"   Days before event:      {days_before} (default)")

    print(f"\n⏳ Checking TicketCity inventory position...")
    tc_share = get_ticketcity_market_share(event_id, info)
    if tc_share and tc_share['tc_blocks'] > 0:
        print(f"   TicketCity holds {tc_share['tc_blocks']} blocks "
              f"({tc_share['listing_share']}% of market listings)")
    else:
        print(f"   No TicketCity inventory found for this event")

    print(f"\n⏳ Finding comparable events...")
    comps, confidence = find_comps(
        event_id,
        info         = info,
        n_comps      = n_comps,
        market_price = market['avg_order_size'] if market else current_price,
    )

    max_comp_price = comps['avg_order_size'].max() if not comps.empty else 0
    price_outlier = current_price > max_comp_price * 3

    if price_outlier:
        print("\n⚠️  PRICE OUTLIER: No historical comps near this price range.")
        print("   Recommendation suppressed — insufficient comparable data.")
        print(f"   Target price: ${current_price:.0f} | "
              f"Max comp price: ${max_comp_price:.0f}")
        return {}

    WEAK_COMP_CATEGORIES = {'Comedy', 'NCAA Basketball', 'Rock'}
    if info['Category_Name'] in WEAK_COMP_CATEGORIES and confidence == 'LOW':
        print(f"\n⚠️  CATEGORY WARNING: {info['Category_Name']} has structurally "
              f"unreliable comp pools. Treat recommendation as directional only.")

    # Cache check
    comp_hash  = abs(hash(tuple(sorted(comps['EventID'].tolist())))) % 100000
    cache_path = TRAJECTORIES_DIR / f"{event_id}_{comp_hash}_curves.parquet"
    norm_path  = TRAJECTORIES_DIR / f"{event_id}_{comp_hash}_norm.parquet"

    if cache_path.exists():
        age_hours = (
            datetime.now() -
            datetime.fromtimestamp(cache_path.stat().st_mtime)
        ).total_seconds() / 3600
        if age_hours < 24:
            print(f"\n⏳ Loading cached trajectories "
                  f"(built {age_hours:.1f}h ago)...")
            curves  = pd.read_parquet(cache_path)
            norm_df = pd.read_parquet(norm_path) \
                      if norm_path.exists() else pd.DataFrame()
        else:
            curves  = None
            norm_df = None
    else:
        curves  = None
        norm_df = None

    if curves is None:
        print(f"\n⏳ Building price trajectories...")
        comp_ids = comps['EventID'].tolist()
        raw_df   = pull_daily_trajectories(comp_ids)
        if raw_df.empty:
            print("❌ No trajectory data found.")
            return {}
        norm_df = normalize_trajectories(raw_df)
        curves  = build_aggregate_curves(norm_df)
        curves.to_parquet(cache_path, index=False)
        norm_df.to_parquet(norm_path, index=False)
        comps.to_csv(
            TRAJECTORIES_DIR / f"{event_id}_{comp_hash}_comps.csv",
            index=False
        )
        print(f"   Trajectories cached.")

    # Sell-through analysis
    print(f"\n⏳ Computing sell-through metrics...")
    if norm_df is not None and not norm_df.empty:
        # Re-pull raw to get Orders column (norm_df may not have it)
        raw_for_st = pull_daily_trajectories(comps['EventID'].tolist()) \
                     if norm_df.empty else norm_df
        # Use the norm_df which has Orders if available
        if 'Orders' in norm_df.columns:
            st_df      = compute_sellthrough_metrics(norm_df)
            st_summary = aggregate_sellthrough(st_df)
        else:
            comp_ids   = comps['EventID'].tolist()
            raw_df2    = pull_daily_trajectories(comp_ids)
            st_df      = compute_sellthrough_metrics(raw_df2)
            st_summary = aggregate_sellthrough(st_df)
    else:
        st_df      = pd.DataFrame()
        st_summary = {}

    if st_summary:
        print(f"   Typical sell pattern: "
              f"{st_summary['median_early_pct']}% early (60+ days) | "
              f"{st_summary['median_mid_pct']}% mid | "
              f"{st_summary['median_late_pct']}% late (0-14 days)")
        print(f"   Peak demand typically: "
              f"day {st_summary['typical_peak_day']:.0f} before event")

    # Demand signal
    demand = compute_demand_signal(curves, days_before, st_summary)
    print(f"   Demand signal: {demand['signal']}")

    # ── Decay risk classification ─────────────────────────────────
    print(f"\n⏳ Classifying decay risk...")
    comp_ids_for_decay = comps['EventID'].tolist() if not comps.empty else []
    decay = classify_decay_risk(comp_ids_for_decay)
    print(f"   Decay class: {decay['decay_class']} | Flag: {decay['timing_flag']}")

    # ── Section analysis ──────────────────────────────────────────
    section = run_section_analysis(event_id)

    print(f"\n⏳ Generating recommendation...")
    rec = generate_recommendation(
        curves            = curves,
        current_price     = current_price,
        days_before_event = days_before,
        cost_basis        = cost_basis,
        mode              = detect_mode(info['Category_Name']),
    )

    # ── Reconcile decay timing_flag with actual rec position ──────────────
    # classify_decay_risk only sees comp aggregates; it doesn't know whether
    # the current price is already above the fitted peak for THIS event.
    # Override HOLD_THROUGH_PEAK when the fitted trajectory shows no upside.
    if decay and rec and decay.get('timing_flag') == 'HOLD_THROUGH_PEAK':
        upside   = rec.get('upside_pct', 0)
        day0_pct = rec.get('day0_pct', 0)
        med_peak = decay.get('med_peak_uplift', 0)
        if upside <= 0:
            # Current price is at or above the fitted peak — holding adds no value
            if day0_pct <= -10:
                decay['timing_flag'] = 'SELL_EARLY_REQUIRED'
                decay['guidance'] = (
                    f"Comps historically peak ~{med_peak:.0f}% above baseline, but your "
                    f"current price already reflects that premium. The fitted trajectory "
                    f"shows {abs(day0_pct):.1f}% downside by event day — sell now to "
                    f"capture current value rather than waiting for a peak that has passed."
                )
            else:
                decay['timing_flag'] = 'SELL_ANYTIME'
                decay['guidance'] = (
                    f"Comps historically peak ~{med_peak:.0f}% above baseline, but your "
                    f"current price is already at the forecast peak level. "
                    f"Sell timing is flexible — no further material upside expected."
                )

    print_report(info, comps, confidence, market, rec,
                 tc_share=tc_share,
                 demand=demand,
                 st_summary=st_summary,
                 decay=decay,
                 section=section)

    return {
        'event_id':   event_id,
        'info':       info,
        'comps':      comps,
        'confidence': confidence,
        'market':     market,
        'curves':     curves,
        'rec':        rec,
        'tc_share':   tc_share,
        'demand':     demand,
        'st_summary': st_summary,
        'decay':      decay,
        'section':    section,
    }
# ================================================================== #
#  ENTRY POINT                                                         #
# ================================================================== #
if __name__ == "__main__":

    print("\n" + "="*65)
    print("  TICKETCITY PRICING ANALYZER")
    print("="*65)

    # EventID
    event_input = input("\nEnter EventID: ").strip()
    if not event_input.isdigit():
        print("❌ Invalid EventID.")
        sys.exit(1)
    event_id = int(event_input)

    # Current listed price — optional, defaults to MinPrice
    price_input = input(
        "Your current listed price per ticket (press Enter to use MinPrice): "
    ).strip()
    if price_input:
        try:
            listed_price = float(price_input)
            if listed_price <= 0:
                raise ValueError
        except ValueError:
            print("  Invalid price entered — will use MinPrice as default.")
            listed_price = None
    else:
        listed_price = None

    # Cost basis — optional
    cost_input = input(
        "Cost basis per ticket (press Enter to skip): "
    ).strip()
    cost_basis = float(cost_input) if cost_input else None

    run_pipeline(
        event_id     = event_id,
        listed_price = listed_price,
        cost_basis   = cost_basis,
    )