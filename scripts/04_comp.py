from google.cloud import bigquery
import pandas as pd
from datetime import datetime, timezone
import re

client = bigquery.Client(project="ticketcity-tcg")
PARTITION = "2026-04-03"
TODAY = datetime.now(timezone.utc).strftime('%Y-%m-%d')

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
#  EXCLUSION FILTERS                                                   #
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
# Exclude higher-tier playoff games from regular season comps
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
# Min orders for a comp to be valid
MIN_ORDERS = {
    'sports':  30,
    'concert': 20,   # lowered — performer ID filter already narrows scope
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

# Recency cutoff for bonus points (within last 18 months)
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
    if category in ANNUAL_CATEGORIES:    return 'annual'
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
    name_lower = name.lower()
    preseason_kws = ['preseason', 'pre-season', 'exhibition',
                     'spring training', 'scrimmage']
    if any(kw in name_lower for kw in preseason_kws):
        return 'preseason'
    tier, _ = detect_tournament_tier(name)
    return tier if tier else 'regular'

def parse_event_date(date_str):
    """Parse 75_Event_Daily Date string to comparable format."""
    if not date_str:
        return None
    for fmt in ['%m/%d/%y %I:%M %p', '%m/%d/%Y %I:%M %p']:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None

# ================================================================== #
#  DATA FETCHERS                                                       #
# ================================================================== #
def get_event_info(event_id: int) -> dict:
    """
    Pulls event metadata and performers in a single efficient query.
    Uses ORDER BY _PARTITIONTIME DESC to get most recent partition.
    """
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
        AND ep._PARTITIONTIME = e._PARTITIONTIME
    LEFT JOIN `data-ticketcity.VividIntake.Performers` p
        ON ep.Performer_ID = p.Performer_ID
        AND p._PARTITIONTIME = e._PARTITIONTIME
    LEFT JOIN `data-ticketcity.VividIntake.Categories` c
        ON e.CategoryID = c.Category_ID
        AND c._PARTITIONTIME = e._PARTITIONTIME
    LEFT JOIN `data-ticketcity.VividIntake.Venues` v
        ON e.VenueID = v.Venue_ID
        AND v._PARTITIONTIME = e._PARTITIONTIME
    WHERE e.EventID = {event_id}
    ORDER BY e._PARTITIONTIME DESC
    LIMIT 20
    """

    df = client.query(query).to_dataframe()

    if df.empty:
        return None

    # Take first row for event-level fields
    row = df.iloc[0]

    # Collect all performers
    performers_df = df[['Performer_ID', 'Master', 'Performer_Name']].dropna(
        subset=['Performer_ID']
    ).drop_duplicates(subset=['Performer_ID'])

    return {
        'EventID':       int(event_id),
        'Name':          str(row['Name'] or ''),
        'LocalDate':     str(row['LocalDate'] or ''),
        'VenueID':       int(row['VenueID'] or 0),
        'Venue_Name':    str(row['Venue_Name'] or ''),
        'City':          str(row['City'] or ''),
        'State':         str(row['State'] or ''),
        'EventType':     str(row['EventType'] or ''),
        'TicketCount':   int(row['TicketCount'] or 0),
        'ListingCount':  int(row['ListingCount'] or 0),
        'MinPrice':      float(row['MinPrice'] or 0),
        'MaxPrice':      float(row['MaxPrice'] or 0),
        'CategoryID':    int(row['CategoryID'] or 0),
        'Category_Name': str(row['Category_Name'] or ''),
        'performers_df': performers_df,
    }


def get_event_profile(event_id: int):
    """
    Wrapper that returns a pandas Series-like object for
    backwards compatibility with existing code.
    """
    info = get_event_info(event_id)
    if info is None:
        return None
    # Return as a simple namespace so dot/bracket access works
    return type('Profile', (), info)()


def get_performers(event_id: int) -> pd.DataFrame:
    """
    Returns performers DataFrame from cached get_event_info call.
    Avoids a second query — get_event_info already pulls performers.
    """
    info = get_event_info(event_id)
    if info is None:
        return pd.DataFrame()
    return info['performers_df']


def get_performer_event_ids_from_daily(performer_names: list):
    """
    Look up historical EventIDs (PIDs) from 75_Event_Daily by performer
    name. Avoids the partition problem with VividIntake — 75_Event_Daily
    has the full historical record.
    Drops category filter so cross-category artists (e.g. Zach Bryan
    filed as Country but event is Pop) are captured correctly.
    """
    if not performer_names:
        return []

    name_conditions = " OR ".join([
        f"LOWER(Name) LIKE '%{n.lower().replace(chr(39), chr(39)+chr(39))}%'"
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
    print(f"  → Performer name lookup: {len(pids)} historical PIDs found")
    return pids


def evaluate_confidence(comps, venue_tier, mode, target_listings):
    if comps is None or comps.empty:
        return 'NONE', 'No comps found'

    best_score = comps['similarity_score'].max()
    n_comps    = len(comps)
    avg_orders = comps['total_orders'].mean()
    max_comp_listings = comps.get('total_quantity', pd.Series([0])).max()

    notes = (f"Best score: {best_score} | "
             f"Comps: {n_comps} | "
             f"Avg orders: {avg_orders:.0f}")

    warnings = []

    # Scale outlier: target is >2x larger than biggest comp
    if max_comp_listings > 0 and target_listings > max_comp_listings * 2:
        warnings.append(
            f"⚠️  SCALE OUTLIER: Target has {target_listings:.0f} listings "
            f"vs comp max {max_comp_listings:.0f} — predictions less reliable"
        )

    # Unprecedented venue/mode combo
    if venue_tier == 'stadium' and mode == 'theater':
        warnings.append(
            "⚠️  OUTLIER: Stadium-scale theater — no reliable historical comps"
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

    # Downgrade if scale outlier flagged
    if any('SCALE OUTLIER' in w or 'OUTLIER' in w for w in warnings):
        if level == 'HIGH':
            level = 'MEDIUM'

    return level, notes


# ================================================================== #
#  TIERED THEATER FALLBACK                                             #
# ================================================================== #
def theater_fallback_comps(p, show_name_clean, min_price,
                           listing_low, listing_high,
                           excl_sql, min_orders, n_comps):
    """
    3-tier fallback for theater when primary query returns < 5 comps:
    Tier 1: Exact show name match (already tried)
    Tier 2: Show name without subtitle/qualifier
    Tier 3: Same venue + price tier
    Tier 4: Same category + price tier
    """
    # Tier 2: strip subtitles after ' - ' or ':'
    base_name = re.split(r' - | in Concert| Live| The Musical',
                         show_name_clean)[0].strip()

    tiers = [
        # (label, name_filter, category_filter)
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
                Venue,
                City,
                State,
                Category,
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
                PID                 AS EventID,
                Name,
                EventDate,
                Venue,
                City,
                State,
                Category,
                total_revenue,
                total_orders,
                total_quantity,
                avg_order_size,
                first_report,
                last_report,
                (
                    CASE WHEN avg_order_size BETWEEN {min_price*0.3}
                                                 AND {min_price*4.0}
                         THEN 4 ELSE 0 END +
                    CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                                 AND {listing_high*3.0}
                         THEN 2 ELSE 0 END +
                    CASE WHEN total_orders > 50 THEN 2 ELSE 0 END +
                    CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 2 ELSE 0 END +
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
            print(f"  → Found {len(df)} comps at fallback tier: '{label}'")
            return df, label

    return pd.DataFrame(), 'none'


# ================================================================== #
#  CORE COMP FINDER                                                    #
# ================================================================== #
def find_comps(event_id: int, n_comps: int = 30):

    # ── Profile ─────────────────────────────────────────────────── #
    p = get_event_info(event_id)
    if p is None:
        print(f"❌ Event {event_id} not found.")
        return None, None, 'NONE'

    performers   = p['performers_df']
    category     = str(p['Category_Name'])
    name         = str(p['Name'])
    min_price    = float(p['MinPrice'])
    max_price    = float(p['MaxPrice'])
    listings     = float(p['ListingCount'])
    mode         = detect_mode(category)
    venue_tier   = get_venue_tier(listings)
    gender       = detect_gender(name)
    sport_sub    = detect_sport_subtype(name)
    tour_tier, _ = detect_tournament_tier(name)
    golf_round   = detect_golf_round(name) if mode == 'annual' else None

    masters      = performers[performers['Master'] == True]
    supporting   = performers[performers['Master'] == False]
    master_ids   = masters['Performer_ID'].tolist()
    master_names = masters['Performer_Name'].tolist()
    away_names   = supporting['Performer_Name'].tolist()

    venue_range  = VENUE_TIER_RANGES[venue_tier]
    listing_low  = venue_range[0]
    listing_high = venue_range[1] if venue_range[1] != float('inf') else 999999
    min_orders   = MIN_ORDERS[mode]

    print(f"\n{'='*65}")
    print(f"TARGET:      {name}")
    print(f"Category:    {category} | Mode: {mode.upper()}")
    print(f"Venue Tier:  {venue_tier} ({listings:.0f} listings)")
    print(f"Gender:      {gender} | Subtype: {sport_sub}")
    print(f"Venue:       {p['Venue_Name']}, {p['City']}, {p['State']}")
    print(f"Date:        {p['LocalDate']}")
    print(f"Price Range: ${min_price} - ${max_price}")
    print(f"Headliners:  {master_names}")
    if tour_tier:   print(f"Tourney Tier:{tour_tier}")
    if golf_round:  print(f"Golf Round:  {golf_round}")
    print(f"{'='*65}")

    # ── Build exclusions ─────────────────────────────────────────── #
    exclusions = (list(UNIVERSAL_EXCLUSIONS) +
                  list(COVID_EXCLUSIONS) +
                  list(BUNDLE_EXCLUSIONS))

    if mode == 'sports':
        if gender == 'mens':
            exclusions += MENS_EXCLUSIONS
        elif gender == 'womens':
            exclusions += WOMENS_EXCLUSIONS
        # Exclude preseason for regular/playoff target
        if sport_sub in ['regular', 'championship', 'semifinal',
                         'quarterfinal', 'tournament_early']:
            exclusions += PRESEASON_EXCLUSIONS
        # Exclude higher playoff tiers for regular season target
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

    # ── Mode-specific logic ──────────────────────────────────────── #
    name_filter  = ""
    extra_filter = ""
    fallback_used = None

    # ── ANNUAL (Golf, Racing, Tennis) ────────────────────────────── #
    if mode == 'annual':
        tournament = (master_names[0] if master_names
                      else name.split('-')[0].strip())
        # Strip sport word for broader matching
        # e.g. "US Open Golf" → "US Open"
        base_tournament = re.sub(
            r'\b(Golf|Tennis|Racing|NASCAR|F1)\b', '',
            tournament, flags=re.IGNORECASE
        ).strip()

        # Round filter — match same round type
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

        name_filter = f"AND LOWER(Name) LIKE '%{base_tournament.lower()}%'"

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
            CASE WHEN avg_order_size BETWEEN {min_price*0.3}
                                         AND {min_price*4.0}
                 THEN 3 ELSE 0 END +
            CASE WHEN total_orders > 100 THEN 2 ELSE 0 END +
            CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 2 ELSE 0 END +
            CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
        """
        # Filter out future events using parsed date
        extra_filter += f"\nAND Report_Date < '{TODAY}'"

    # ── SPORTS ──────────────────────────────────────────────────── #
    elif mode == 'sports':

        if tour_tier in ['championship', 'semifinal']:
            # Championship/semifinal: tier keywords are primary signal
            # Weight championship keywords heavily — more than venue/city
            tier_kws = TOURNAMENT_TIERS.get(tour_tier, [])
            kw_cases = "\n+".join([
                f"CASE WHEN LOWER(Name) LIKE '%{kw.lower()}%' THEN 5 ELSE 0 END"
                for kw in tier_kws
            ])
            scoring = f"""
                {kw_cases} +
                CASE WHEN Category = '{category}' THEN 3 ELSE 0 END +
                CASE WHEN avg_order_size BETWEEN {min_price*0.25}
                                             AND {min_price*5.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN total_orders > 300 THEN 2 ELSE 0 END +
                CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END +
                CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
            """
            # Must match at least one tier keyword
            kw_conditions = " OR ".join([
                f"LOWER(Name) LIKE '%{kw.lower()}%'"
                for kw in tier_kws
            ])
            extra_filter = f"AND ({kw_conditions})"

        elif sport_sub in ['quarterfinal', 'tournament_early']:
            tier_kws = TOURNAMENT_TIERS.get(sport_sub, [])
            kw_cases = "\n+".join([
                f"CASE WHEN LOWER(Name) LIKE '%{kw.lower()}%' THEN 3 ELSE 0 END"
                for kw in tier_kws
            ]) if tier_kws else "0"
            scoring = f"""
                {kw_cases} +
                CASE WHEN Category = '{category}' THEN 2 ELSE 0 END +
                CASE WHEN avg_order_size BETWEEN {min_price*0.3}
                                             AND {min_price*4.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN total_orders > 100 THEN 2 ELSE 0 END +
                CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
            """

        else:
            # Regular season: home team is primary signal
            home_team  = master_names[0] if master_names else ""
            away_team  = away_names[0] if away_names else ""

            # Use first meaningful word of team name
            home_word = home_team.split()[0] if home_team else ""
            away_word = away_team.split()[0] if away_team else ""

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
                CASE WHEN avg_order_size BETWEEN {min_price*0.4}
                                             AND {min_price*3.0}
                     THEN 2 ELSE 0 END +
                CASE WHEN total_quantity BETWEEN {listing_low}
                                             AND {listing_high*1.5}
                     THEN 1 ELSE 0 END +
                CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END
            """

    # ── CONCERT ─────────────────────────────────────────────────── #
    elif mode == 'concert':
        # Use 75_Event_Daily performer name lookup — avoids partition
        # and category mismatch issues
        historical_pids = get_performer_event_ids_from_daily(master_names)

        if historical_pids:
            pids_str    = ", ".join(str(i) for i in historical_pids)
            # Hard filter on PIDs — no category restriction
            name_filter = f"AND PID IN ({pids_str})"
            # Remove category filter from WHERE since PIDs already narrow scope
            category_filter = ""

            scoring = f"""
                CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                             AND {listing_high*3.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN avg_order_size BETWEEN {min_price*0.3}
                                             AND {min_price*4.0}
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
            # Fallback: name matching + genre
            print("  → No performer history — falling back to name + genre")
            category_filter = f"AND Category = '{category}'"
            artist_cases = "\n+".join([
                f"CASE WHEN LOWER(Name) LIKE '%{n.lower().replace(chr(39), chr(39)*2)}%' THEN 5 ELSE 0 END"
                for n in master_names
            ]) if master_names else "0"
            scoring = f"""
                {artist_cases} +
                CASE WHEN avg_order_size BETWEEN {min_price*0.3}
                                             AND {min_price*4.0}
                     THEN 3 ELSE 0 END +
                CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                             AND {listing_high*3.0}
                     THEN 2 ELSE 0 END +
                CASE WHEN total_orders > 200 THEN 2 ELSE 0 END +
                CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END
            """

    # ── THEATER ─────────────────────────────────────────────────── #
    elif mode == 'theater':
        category_filter = f"AND Category = '{category}'"
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
            CASE WHEN avg_order_size BETWEEN {min_price*0.3}
                                         AND {min_price*4.0}
                 THEN 4 ELSE 0 END +
            CASE WHEN total_quantity BETWEEN {listing_low*0.3}
                                         AND {listing_high*3.0}
                 THEN 2 ELSE 0 END +
            CASE WHEN total_orders > 50  THEN 3 ELSE 0 END +
            CASE WHEN total_orders > 20  THEN 1 ELSE 0 END +
            CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 2 ELSE 0 END +
            CASE WHEN City = '{p['City']}' THEN 1 ELSE 0 END
        """

    # ── GENERIC FALLBACK ─────────────────────────────────────────── #
    else:
        category_filter = f"AND Category = '{category}'"
        scoring = f"""
            CASE WHEN avg_order_size BETWEEN {min_price*0.4}
                                         AND {min_price*3.0}
                 THEN 3 ELSE 0 END +
            CASE WHEN total_quantity BETWEEN {listing_low}
                                         AND {listing_high}
                 THEN 2 ELSE 0 END +
            CASE WHEN last_report >= '{RECENCY_CUTOFF}' THEN 1 ELSE 0 END
        """

    # For non-concert modes set category_filter if not already set
    if mode != 'concert' and 'category_filter' not in dir():
        category_filter = f"AND Category = '{category}'"

    # ── Execute main query ───────────────────────────────────────── #
    comps_query = f"""
    WITH raw AS (
        SELECT
            PID,
            Name,
            Date                AS EventDate,
            Venue,
            City,
            State,
            Category,
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
          {category_filter if mode != 'concert' or not historical_pids else ''}
          {extra_filter}
        GROUP BY PID, Name, Date, Venue, City, State, Category
        HAVING SUM(Orders) >= {min_orders}
    ),
    scored AS (
        SELECT
            PID                     AS EventID,
            Name,
            EventDate,
            Venue,
            City,
            State,
            Category,
            total_revenue,
            total_orders,
            total_quantity,
            avg_order_size,
            first_report,
            last_report,
            ({scoring})             AS similarity_score
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

    if mode == 'concert' and not comps.empty:
        def is_past_event(date_str):
            dt = parse_event_date(str(date_str))
            if dt is None:
                return True
            return dt < datetime.now()
        comps = comps[comps['EventDate'].apply(is_past_event)].reset_index(drop=True)

    # ── Theater fallback if < 5 comps ───────────────────────────── #
    if mode == 'theater' and len(comps) < 5:
        print(f"  → Only {len(comps)} theater comps — trying fallback tiers")
        comps, fallback_used = theater_fallback_comps(
            p, show_name_clean, min_price,
            listing_low, listing_high,
            excl_sql, min_orders, n_comps
        )

    # ── Filter out future events for annual mode ─────────────────── #
    if mode == 'annual' and not comps.empty:
        def is_past(date_str):
            dt = parse_event_date(str(date_str))
            if dt is None:
                return True
            return dt < datetime.now()
        comps = comps[comps['EventDate'].apply(is_past)].reset_index(drop=True)

    # ── Confidence ───────────────────────────────────────────────── #
    confidence, conf_notes = evaluate_confidence(
        comps, venue_tier, mode, listings
    )

    # ── Output ───────────────────────────────────────────────────── #
    print(f"\nTOP {len(comps)} COMPS "
          f"[{mode.upper()} | {venue_tier.upper()}"
          f"{' | fallback: ' + fallback_used if fallback_used else ''}]:")

    if not comps.empty:
        print(comps[['EventID','Name','EventDate','City',
                      'total_orders','avg_order_size',
                      'similarity_score']].to_string())
    else:
        print("  No comps found.")

    print(f"\n🎯 Confidence: {confidence}")
    print(f"   {conf_notes}")

    if not comps.empty:
        comps.to_csv(f"data/comps_{event_id}.csv", index=False)
        print(f"   Saved to data/comps_{event_id}.csv")

    return p, comps, confidence

# ================================================================== #
#  ENTRY POINT — prompt user or use defaults                           #
# ================================================================== #
if __name__ == "__main__":

    print("\n" + "="*65)
    print("TICKETCITY COMP ENGINE")
    print("="*65)

    # Ask user for input
    print("\nEnter EventID(s) to analyze.")
    print("You can enter a single ID or multiple comma-separated IDs.")
    print("Press Enter with no input to run the default test events.\n")

    user_input = input("EventID(s): ").strip()

    if user_input:
        # Parse user input
        try:
            event_ids = {
                int(eid.strip()): f"EventID {eid.strip()}"
                for eid in user_input.split(",")
                if eid.strip().isdigit()
            }
            if not event_ids:
                print("No valid EventIDs found. Running defaults.")
                raise ValueError
        except ValueError:
            event_ids = {
                5687566: "NCAA Final Four Championship",
                6487949: "NFL Ravens vs Chargers",
                5045286: "US Open Golf Friday",
                6338949: "Zach Bryan",
                6062329: "My Chemical Romance",
                6126790: "Les Miserables",
                5752841: "Monty Python Spamalot",
                6342099: "Shane Gillis Comedy",
            }
    else:
        event_ids = {
            5687566: "NCAA Final Four Championship",
            6487949: "NFL Ravens vs Chargers",
            5045286: "US Open Golf Friday",
            6338949: "Zach Bryan",
            6062329: "My Chemical Romance",
            6126790: "Les Miserables",
            5752841: "Monty Python Spamalot",
            6342099: "Shane Gillis Comedy",
        }

    n_comps = 10  # default comp count

    results = {}
    for event_id, label in event_ids.items():
        print(f"\n{'#'*65}")
        print(f"# {label}")
        print(f"{'#'*65}")
        try:
            # Show event info first
            info = get_event_info(event_id)
            if info:
                print(f"\nEVENT INFO:")
                print(f"  Name:       {info['Name']}")
                print(f"  Date:       {info['LocalDate']}")
                print(f"  Venue:      {info['Venue_Name']}, "
                      f"{info['City']}, {info['State']}")
                print(f"  Category:   {info['Category_Name']}")
                print(f"  Listings:   {info['ListingCount']}")
                print(f"  Price:      ${info['MinPrice']} - "
                      f"${info['MaxPrice']}")
                masters = info['performers_df'][
                    info['performers_df']['Master'] == True
                ]['Performer_Name'].tolist()
                print(f"  Headliners: {masters}")
            else:
                print(f"❌ EventID {event_id} not found.")
                continue

            profile, comps, confidence = find_comps(
                event_id, n_comps=n_comps
            )
            results[event_id] = {
                'label':      label,
                'info':       info,
                'comps':      comps,
                'confidence': confidence
            }
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            results[event_id] = {
                'label':      label,
                'info':       None,
                'comps':      None,
                'confidence': 'ERROR'
            }

    # Confidence summary
    print(f"\n{'='*65}")
    print("CONFIDENCE SUMMARY")
    print(f"{'='*65}")
    for eid, r in results.items():
        n = len(r['comps']) if r['comps'] is not None else 0
        print(f"{r['label']:<35} → {r['confidence']:<8} ({n} comps)")