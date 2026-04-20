# TicketCity Pricing Analysis Project

## Project Overview
Ticket pricing prediction and sell-timing recommendation system for TicketCity.
Given a future EventID, the system finds historical comparable events, builds
price trajectory curves, and recommends optimal sell timing for ticket inventory.

## BigQuery Connection
- Billing project: ticketcity-tcg
- Data project: data-ticketcity
- Python client: `bigquery.Client(project="ticketcity-tcg")`
- Auth: gcloud application-default credentials (already configured)

---

## Key Tables

### data-ticketcity.TC_Data.InventoryStream
- 3.89 BILLION rows — clustered on EventID, NO partition
- ALWAYS filter by exact EventID — never scan without it
- Cost: ~23MB per single EventID query (essentially free)
- Broad scans (time range without EventID) cost 100GB+ — NEVER do this
- Use ONLY for: current/live inventory on active events
- Historical data only goes back to ~January 2025
- Fields: Time (INTEGER unix), BlockId (STRING), EventID (INTEGER),
  DataSource, SectionName, SectionID, Row, Quantity, Price (FLOAT),
  StockType, Splits, SnapshotId, SnapBlockId

### data-ticketcity.TC_Data.TicketCity_Tickets
- Only 4,122 rows — our owned inventory
- `__key__.name` = InventoryStream.BlockId (join key)
- `tfs_event_id` = InventoryStream.EventID
- Updated each morning at 8am CDT
- Use for: checking TicketCity's market share position per event

### data-ticketcity.TFS_Reports.75_Event_Daily
- 27 million rows — NOT partitioned
- PRIMARY SOURCE for all historical trajectory and comp data
- PID = EventID (same ID system as InventoryStream)
- Fields: Name, Date (STRING 'MM/DD/YY H:MM PM'), Venue, City, State,
  Category, Revenue, Avg_Order_Size, Orders, Quantity, Report_Date (DATE)
- Covers 2021-2026, all network sales (not just TicketCity)
- Cost: ~$0.015 per event query, ~$0.30 for 20 comp events
- Parse Date with: SAFE.PARSE_DATE('%m/%d/%y', SUBSTR(Date, 1, 8))
- ALWAYS exclude: Gift Cards, eGift, Parking, Test events, festival events

### data-ticketcity.VividIntake.Events
- 324 million rows — partitioned by DAY on _PARTITIONTIME
- Partition is auto-detected at runtime via `_get_vivid_partition()` (queries INFORMATION_SCHEMA)
- Latest partition = future/upcoming events only — no manual date update needed
- Fields: EventID, CategoryID, EventType, Name, LocalDate (DATETIME),
  IsDateTBD, IsTimeTBD, ListingCount, MinPrice, MaxPrice,
  TicketCount, TimeZone, VenueID

### data-ticketcity.VividIntake.Categories
- Partitioned by DAY — always use partition filter (auto-detected)
- Fields: Event_Type, Category_ID, Category_Name

### data-ticketcity.VividIntake.Venues
- Partitioned by DAY — always use partition filter (auto-detected)
- Fields: Venue_ID, Venue_Name, City, State, Address1,
  Country_Code, Phone, Postalcode, Region_ID, Timezone

### data-ticketcity.VividIntake.Performers
- 438 million rows — partitioned by DAY (auto-detected)
- Fields: Category_ID, Category_Name, Event_Type, Performer_ID, Performer_Name

### data-ticketcity.VividIntake.EventPerformers
- 358 million rows — partitioned by DAY (auto-detected)
- Fields: Event_ID, Master (BOOLEAN), Performer_ID
- Master=TRUE = headline act / home team
- Master=FALSE = supporting act / away team

---

## Critical Join Keys
InventoryStream.EventID
= TFS_Reports.75_Event_Daily.PID
= VividIntake.Events.EventID
= VividIntake.EventPerformers.Event_ID
InventoryStream.BlockId
= TC_Data.TicketCity_Tickets.key.name
VividIntake.EventPerformers.Performer_ID
= VividIntake.Performers.Performer_ID
VividIntake.Events.VenueID    = VividIntake.Venues.Venue_ID
VividIntake.Events.CategoryID = VividIntake.Categories.Category_ID

---

## Cost Control Rules
1. InventoryStream: ALWAYS filter by exact EventID — never scan without it
2. VividIntake tables: ALWAYS include partition filter (auto-detected at runtime)
3. 75_Event_Daily: filter by PID list or Name/Category — never full scan
4. Prefer 75_Event_Daily for ALL historical data over InventoryStream
5. Cache results to data/cache/bigquery/ as pickle — never re-query same data
6. Cap comp pulls at 15 events maximum per analysis run
7. Cache key = hash(inputs + CACHE_VERSION); CACHE_VERSION auto-derived from main.py content hash

## Caching Architecture

### Backend cache (main.py)
- Location: `data/cache/bigquery/` — pickle files, TTL-based expiration
- Key: `_stable_cache_key(prefix, payload)` = MD5(inputs + CACHE_VERSION)
- `CACHE_VERSION` = first 12 chars of MD5(`main.py` file content) via `_compute_code_version()`
  → Any code change automatically invalidates ALL cached results — no manual version bump needed
  → Client on Streamlit never needs to touch main.py to clear stale cache
- TTLs: event_info=6h, market=30m, tc_share=15m, performers=12h, comps=6h,
  trajectories=6h, section=30m, decay=30m
- "Using cache: X" = cache hit; "Cache MISS: X" = BigQuery billed

### Previous billing leaks (now fixed)
- `classify_decay_risk()` and `get_inventory_snapshots()` previously called
  `client.query()` directly with no cache wrapper — re-billed on every pipeline run
- Both now wrapped in `cached_result()`
- `get_inventory_snapshots` key bucketed to calendar date (not second-precision)
  so all calls within the same day share one cache entry

### Frontend cache (frontend.py)
- `@st.cache_data(ttl=1800)` on `run_pipeline()` — 30 min in-memory cache
- Keyed by all function args including listed_price and cost_basis
- Different prices = separate cache entry = full pipeline re-runs (but hits disk cache for BQ)

---

## Project File Structure
ticketcity-pricing/
├── CLAUDE.md                          ← This file
├── venv312/                           ← Virtual environment (Python 3.12)
├── data/
│   ├── schemas.md                     ← Full table schemas
│   ├── backtest/
│   │   └── backtest_YYYY-MM-DD.csv   ← Backtest results
│   └── trajectories/
│       ├── {event_id}_{hash}_curves.parquet
│       ├── {event_id}_{hash}_norm.parquet
│       └── {event_id}_{hash}_comps.csv
└── scripts/
    ├── main.py                        ← PRIMARY SCRIPT — full pipeline
    ├── frontend.py                    ← Streamlit dashboard (client-facing)
    └── backtest.py                    ← Backtesting script

---

## Architecture: How the Pipeline Works (main.py)

### Full Data Flow
User Input: EventID + listed_price (optional) + cost_basis (optional)
↓
Step 1: get_event_info(event_id)
→ VividIntake.Events + EventPerformers + Performers + Categories + Venues
→ Single query using _PARTITIONTIME matching
→ Returns: name, category, venue, date, price range, listing count, performers

Step 2: get_current_market_price(event_id)
→ 75_Event_Daily: most recent 5 rows for this PID
→ Returns: Avg_Order_Size, Orders, days_before_event, report_date
→ Used for CONTEXT ONLY — not as price anchor

Step 3: get_ticketcity_market_share(event_id, info)
→ TC_Data.TicketCity_Tickets: count blocks/tickets for this event
→ Compare to VividIntake ListingCount/TicketCount
→ Returns: tc_blocks, tc_tickets, listing_share%, ticket_share%
→ Currently shown in report but NOT used to modulate recommendations
→ FUTURE: high TC share (>20%) should increase HOLD bias (price setter, not taker)

Step 4: find_comps(event_id, info, n_comps=15, market_price)
→ 75_Event_Daily: find historical comparable events by category/name/performer
→ Mode routing: annual / sports / concert / theater / generic
→ Returns: comps DataFrame + confidence level (HIGH/MEDIUM/LOW)

Step 5: pull_daily_trajectories(comp_ids)
→ 75_Event_Daily: day-by-day sales for comp PIDs
→ Returns raw DataFrame with Orders, Avg_Order_Size, days_before_event

Step 6: normalize_trajectories(df)
→ baseline_price = median Avg_Order_Size at 60-120 days before event
→ price_index = Avg_Order_Size / baseline_price
→ Returns normalized DataFrame

Step 7: build_aggregate_curves(df, bucket_size=7)
→ Weekly buckets, median/p25/p75 price_index, min 2 events per bucket
→ Returns curves DataFrame

Step 8: compute_sellthrough_metrics(df) + aggregate_sellthrough(st_df)
→ From Orders column: early_sell_pct, mid_sell_pct, late_sell_pct
→ peak_velocity_day, velocity_trend (ACCELERATING/STABLE/DECELERATING)
→ Returns per-event and aggregate sell-through summary
→ Surfaced in print_report() under SELL-THROUGH PATTERN section

Step 9: compute_demand_signal(curves, days_before_event, st_summary)
→ Scores demand HIGH/MEDIUM/LOW based on:
   - Orders velocity ratio vs comp average
   - Acceleration/deceleration rate
   - Sell-through pattern alignment with current time horizon
   - Past peak flag
→ Computed but currently NOT wired into recommendation logic
→ FUTURE: HIGH demand signal should increase HOLD threshold by ~5%

Step 10: generate_recommendation(curves, current_price, days_before_event,
                                  prior_price, cost_basis, mode)
→ Accepts mode parameter (detect_mode result) for sports-aware thresholds
→ Sports events use day0_decay_threshold = -30% (vs -20% for concerts/theater)
   because sports fans routinely buy within 7 days of game — normal decay
→ Fits smoothed trajectory via interpolation + 7-day rolling median
→ Infers baseline: current_price / current_index
→ current_price = user's listed_price if provided; defaults to market['avg_order_size']
   (recent actual sale average) — previously defaulted to MinPrice (get-in price)
→ day0_p25_price exposed in output: p25 index at day=0 × baseline
   = "1-in-4 comparable events ended at or below this price by event day"
   Displayed in UI as floor risk — not used as a model anchor
→ Decision logic (priority order):
   1. Realized decline stop-loss: price dropped ≥20% from day-90 AND
      day0_pct < -10% AND upside < 30% → SELL_NOW
      (prior_price anchor prevents tautological re-baseline)
   2. Late window (≤14 days): default SELL_NOW unless strong spike evidence
   3. poor_rr (ratio > 2.0 AND downside < -15%) → SELL_NOW
   4. upside ≥ 10% AND peak_day > 7 → HOLD
   5. upside ≥ 5% AND peak_day > 14 → HOLD
   6. day0 < day0_decay_threshold AND upside < 15% → SELL_NOW
   7. downside < -15% AND upside < 5% AND day0_pct < -10% → SELL_NOW
   8. flat (±5% upside, ±10% downside):
      → SELL_NOW if realized_decline_pct ≤ -12% (observed path overrides)
      → MONITOR otherwise
   9. day0 < -15% AND upside < 10% → SELL_NOW
   10. default:
      → SELL_NOW if realized_decline_pct ≤ -12%
      → MONITOR otherwise
→ P&L: cost_basis, current/peak/day0 profit_pct and profit_abs

Step 11: print_report()
→ Sections: Event header, Market Snapshot, TicketCity Position,
  Category Risk flag, Sell-Through Pattern, Comp Analysis (top 3),
  Price Predictions, Recommendation, P&L Summary, Peak window
→ Category risk flags shown for: Comedy, NCAA Basketball, Rock, Pop, MLB

---

## Comp Engine Logic

### Category Routing
- annual:  PGA Golf, NASCAR Racing, F1 Racing, Tennis, Horse Racing, Motorsports
- sports:  NFL, NCAA, NBA, MLB, NHL, MLS, Soccer, Boxing, Wrestling, etc.
- concert: Rock, Pop, Country, R&B, Hip Hop, Alternative, etc.
- theater: Broadway, Comedy, Arts and Theater, Musical, Opera, etc.

### Universal Exclusions (always applied to comp queries)
Test events, Parking, Gift Cards, eGift, Vouchers, Packages,
COVID/Reduced Capacity events, Bundle/All Session products

### Additional Backtest Candidate Exclusions
NBA Cup, In-Season Tournament, Cup Championship, London Games,
Mexico City Games, Paris Games, Madrid Games, Global Series,
Opening Series, NFL Draft, Celebrity games, Rising Stars,
Rookie games — all excluded from find_diverse_candidates()
These have atypical price curves that corrupt comp trajectories.

### Sports Mode
- Championship/Semifinal: match by tournament tier keywords
  (Final Four, Championship Game, Super Bowl, World Series, etc.)
- Regular season: match by home team name (Master performer) + city OR venue
- Neutral-site games (e.g. Red River rivalry): when VividIntake has no Master=TRUE
  performer, `home_word` is empty → venue name used as the hard filter instead
  of passing all same-category events through (previous bug: OU away games appeared
  as Texas comps because geographic filter was silently skipped)
- Excludes: preseason games, opposite gender events,
  higher playoff tiers when target is regular season

### Concert Mode
- Primary: performer name lookup in 75_Event_Daily (no category filter)
  Fixes cross-genre artists (e.g. Zach Bryan filed as Country but event is Pop)
- Scoring: same venue (+3), high orders (+3), recency (+2)
- Fallback: name + genre matching if no performer history found
- Festival and multi-artist showcase exclusions applied UNLESS target is itself a festival
  (bidirectional: solo show ↔ festival never cross-contaminates)

### Festival & Multi-Artist Showcase Exclusions
- `FESTIVAL_EXCLUSIONS` — SQL LIKE patterns applied at comp discovery time
- `_FESTIVAL_KEYWORDS` — Python set used to detect whether the TARGET is a festival
- Bidirectional logic: if `is_festival_target` is True, exclusions are NOT applied
  (so a festival can still find festival comps)
- Covers: named festivals (Lollapalooza, Coachella, Rolling Loud, etc.),
  multi-artist radio showcases (Jingle Ball, iHeartRadio, Wango Tango),
  day/weekend passes, and similar rotating-lineup events
- Also applied inside `pull_daily_trajectories()` to strip festival events
  from trajectory data regardless of comp mode
- Root cause this fixed: Kid Cudi returning "2022 Rolling Loud - 3 Day Pass"
  as its top comp; Demi Lovato returning "iHeartRadio Jingle Ball" ($633 avg)
  which inflated trajectory baseline vs solo show prices (~$124–$290)

### Theater Mode
- Primary: exact show name match
- Fallback tiers: base name → same venue → same category
- Triggers when < 5 comps found from primary

### Annual Mode (Golf/Racing)
- Match by tournament name (strip sport word: "US Open Golf" → "US Open")
- Round matching: Thursday/Friday comps for weekday, Saturday/Sunday for weekend
- Filter out future events by parsing EventDate string vs TODAY

### Two-Tier Comp System
- Tier 1 — Same Event: name token overlap, weight=1.0, max 5 comps
- Tier 2 — Structural comps: filtered by similarity + z-score, weight=0.3–1.0
  Drops events >1.5 std deviations from comp mean (outlier suppression)
- Fallback: if no Tier 1, all comps weighted equally
- Impact: reduces trajectory noise from single outlier comps

### Confidence Levels
- HIGH:   best similarity score ≥ 8, n_comps ≥ 8
- MEDIUM: best score ≥ 5, n_comps ≥ 4
- LOW:    below medium thresholds
- Downgraded if scale outlier detected (target listings > 2x comp max)
- If target price > 3x max comp avg_order_size → suppress recommendation
  entirely (INSUFFICIENT_DATA) — no valid comp precedent exists

### Venue Size Tiers (from ListingCount)
- stadium:     2000+ listings
- large_arena: 800–2000
- arena:       300–800
- theater:     100–300
- small:       0–100

---

## Backtesting Architecture (backtest.py)

### Design Principles
- Strict time isolation: comps only use data available BEFORE snapshot_date
- Comps must have ENDED before snapshot_date (no future event leakage)
- Sequential simulation: walks through [90, 60, 30, 14] day checkpoints
- prior_price anchor = baseline_price (day-90 price) — NEVER updated mid-run
  This prevents false stop-loss triggers from mid-trajectory spikes
- Day-90 SELL_NOW demoted to MONITOR_90 — wait for day-60 confirmation
- Day-60 SELL_NOW requires confirmation: snapshot_price ≤ baseline * 0.95
  OR downside_pct < -25% — prevents selling into comp-trajectory noise
- Day-30 and Day-14: no confirmation gate — model decision is final
- First SELL_NOW after gates pass = model sell date
- If never SELL_NOW → sell at event day (actual day-0 price)
- Baseline: median price in ±14 day window around day 90
- Sanity check: skip events where baseline > 1.6x event avg price
  (tightened from 2.0x — catches speculative day-90 peaks before they
  corrupt the baseline comparison)

### Backtest Candidate Selection (find_diverse_candidates)
- Stratified by city + year: max 2 events per city per year
- Must have data from ≥60 days before AND ≤7 days before event
- Must be completed events (event date in past)
- Minimum 50 orders and 5 data points
- Excludes: All-Star games, International Series, Hall of Fame,
  Summer League, Pro Bowl, NBA Cup, London/Mexico/Paris games
- Recommended pool size: 12 events per category (not 5)
  5-event pools produce too much variance across runs
  12-event pools give stable enough signal to act on

### Summary Metrics (print_summary)
- Raw avg vs baseline: primary metric (can be skewed by tail events)
- Winsorized avg (±$500 cap): added — reflects typical performance
- Median vs baseline: added — most robust to outliers
- Beat rate vs day-0: % of events where model beats holding to event day
- All three reported per category in winsorized results block

### Performance Benchmarks (across 6 runs, ~300 total events)
Stable findings (consistent across ≥4 of 6 runs):
- NFL:  ~55-60% beat baseline, near-zero to positive avg — RELIABLE
- NBA:  ~50-55% beat baseline, positive avg when Lakers outliers present
        High variance — treat individual run results cautiously
- MLB:  45-67% beat baseline, near-zero avg — DIRECTIONALLY USEFUL
        Improved significantly after day-60 confirmation gate added
- NHL:  27-58% beat baseline, highly volatile — UNRELIABLE
        Many events have anomalous day-90 price peaks (playoff fever,
        seasonal spikes) — 1.6x sanity check helps but doesn't fully solve
- NCAA Basketball: 0-27% beat baseline, consistently negative avg
        STRUCTURAL FAILURE — category-level comps cannot capture
        game-specific rivalry/importance demand spikes
        Treat all NCAA recommendations as LOW confidence
- Rock: 20-50% beat baseline, consistently negative avg
        Artist-specific demand not captured by genre comps
- Pop:  18-75% beat baseline, extreme variance
        Outlier events (Taylor Swift, Big Time Rush) dominate averages
        Winsorized avg is the only meaningful metric for this category
- Comedy: 0-20% beat baseline, consistently negative avg
        Same structural problem as Rock — artist specificity

### What NOT to tune based on backtest results
- Do not adjust thresholds based on individual run outcomes
  With 12 events per category, one bad draw changes results by 20%+
- Do not add new checkpoint gates without running ≥3 large backtests
  The day-30 gate was added and reverted — it made things worse
  because it let through only the confirmed-bad declines
- The ±$500 winsorization cap is for reporting only — do not filter
  outliers from the actual backtest training data

---

## Key Model Decisions Made in This Thread

### Changes that improved performance (kept)
1. Day-60 confirmation gate: only sell at day 60 if price has declined
   ≥5% from baseline OR downside_pct < -25%
   → Reduced day-60 trigger rate from 71% to 54%, improved MLB significantly
2. Sports-aware day0_pct threshold: -30% for sports, -20% for concerts
   → Sports fans buy within final week — 30% day-of decay is normal
3. MONITOR override on observed decline: if realized_decline_pct ≤ -12%,
   override MONITOR → SELL_NOW regardless of comp trajectory
   → Fixes the core complaint: model watching real prices fall while saying MONITOR
4. Price outlier gate: suppress recommendation if target price > 3x
   max comp price — outputs INSUFFICIENT_DATA instead of bad recommendation
   → Prevents Taylor Swift / Matt Rife type catastrophic misfires
5. Category risk flags in print_report(): visible warning for weak categories
6. Sell-through pattern in print_report(): surfaces what was previously
   only used internally
7. Winsorized summary metrics in print_summary(): median and ±$500
   winsorized avg added alongside raw avg
8. Tightened sanity check from 2.0x to 1.6x: filters more anomalous
   day-90 baselines before they corrupt the comparison

### Changes that were tried and reverted
1. Day-30 confirmation gate (mirroring day-60 gate):
   REVERTED — made performance worse
   Reason: the gate blocked some bad day-30 sells but the ones that
   passed through (price confirmed declining) were the catastrophic
   ones. Gate was solving wrong problem — day-30 failures come from
   wrong comp trajectories, not unconfirmed price moves.
2. poor_rr threshold change from 2.0 to 2.5:
   NOT APPLIED — too close to sample-fitting on 73 events

### The beat-rate vs EV paradox (important for presentation)
The model beats hold-to-event-day 74-84% of events but still shows
negative EV vs baseline in most runs. This is not a contradiction —
it is negative skew. Wins are small (+$30-80 typical), losses are
large (Taylor Swift: -$3,027 in one run). The model's primary value
is DOWNSIDE PROTECTION, not alpha generation vs aggressive early sale.
Frame this correctly for TicketCity: the model reliably prevents
the worst outcome (holding too long on a decaying event), not that
it reliably beats selling everything at day 90.

---

## Category-Specific Notes

### NFL — works well
Liquid comp pool, consistent game-to-game demand curves.
Home team matching gives strong comp signal.
Watch for: Thanksgiving/Christmas/primetime games have inflated
day-90 prices — check baseline vs avg price ratio.

### NBA — works but volatile
Lakers road games appear frequently in backtest (high liquidity/orders).
Non-Lakers games have thinner comp pools and worse performance.
New Year's Eve games and rivalry matchups spike unpredictably.

### MLB — directionally useful
Flat price decay toward event day is normal, not alarming.
Bobblehead/promotional games inflate early prices — check event name.
Mexico City series games are niche events with no valid comps — exclude.
Promotionally-priced games (Free Shirt Friday etc.) have inflated
day-90 baselines that crash — the 1.6x sanity check catches most.

### NHL — unreliable
High variance within category. Many events have anomalous day-90
peaks driven by playoff contention uncertainty at time of listing.
Winter Classic and outdoor games are completely different demand
curves — should be excluded from regular-season comp pools.
Boston Bruins road games consistently appear as high-price outliers.

### NCAA Basketball — do not rely on model
Category-level comps fundamentally cannot capture:
- Rivalry game demand (Duke/UNC vs routine conference game)
- Late-season importance (bubble games, senior nights)
- Tournament proximity effects
Recommendation: flag all NCAA events as INSUFFICIENT_DATA or
explicitly tell TicketCity the model does not cover this category.
Fix requires game-importance features (rivalry flag, standings data)
that are not currently in the data pipeline.

### Rock / Comedy / Pop — artist-specific, not genre-level
The comp engine finds genre-level historical events, but demand for
a specific artist is not captured. A Jerry Seinfeld comp pool
that includes Tim Hawkins misrepresents demand curves entirely.
Performer-level comps (already implemented for concerts) help but
require the performer to have prior TicketCity sales history.
New/emerging artists and niche acts will always have LOW confidence.

---

## Deliverables Status

### Deliverable 1 — Inventory timing & market dynamics analysis
PARTIAL
✅ Historical pricing patterns across event types (2021–2025)
✅ Sell-through rates computed and surfaced in report
✅ Event type segmentation with distinct comp logic per mode
⚠️ TicketCity market share computed but not used in recommendations
❌ Section-level analysis (GA vs reserved) — Phase 2
❌ Cross-marketplace inventory movement — only Vivid+TC data used

### Deliverable 2 — Early-sale vs hold strategy evaluation
PARTIAL
✅ Backtester quantifies model vs sell-at-90 vs hold-to-event
✅ Upside/downside percentages and price predictions per event
✅ Winsorized mean and median added to separate typical vs tail outcomes
⚠️ Downside probability not expressed as explicit % (e.g. "40% chance
   of >15% loss") — currently implicit in downside_pct field
⚠️ Opportunity cost implicit in comparison but not stated per-ticket
❌ Inventory block-level analysis — model is per-event, not per-block

### Deliverable 3 — Predictive pricing & sell-timing model
MOSTLY COMPLETE
✅ Price trajectory model with normalized comp curves + p25/p75 bands
✅ SELL_NOW/HOLD/MONITOR recommendation with full reasoning
✅ Confidence gate — suppresses recommendation when no valid comps
✅ Category risk flags in report output
✅ MONITOR override on observed price decline
✅ Sports-aware decay threshold
✅ Sell-through pattern surfaced in report
⚠️ TicketCity inventory share computed but not in recommendation logic
⚠️ Demand signal computed but not used to modify recommendation

### Deliverable 4 — Strategy recommendations & profit optimization
PARTIAL
✅ Category-level guidance: which categories model is reliable for
✅ Decision framework: sell-at-90 vs model vs hold comparison
⚠️ High-confidence inventory classification exists but not explicit
⚠️ Code documentation exists but no standalone client document
❌ Section-level strategies (GA1 vs upper bowl) — not addressed
❌ Dynamic risk-tolerance parameter for TicketCity to tune

---

## Pending Work — Priority Order

### Priority 1 — Before final presentation
1. Client documentation: 4-5 page written doc covering:
   - What the model does and how to interpret recommendations
   - Which categories are reliable vs not (with backtest evidence)
   - Known limitations and when to override the model
   - How to run main.py and interpret the output sections
2. Wire demand signal into recommendation: HIGH demand signal should
   increase HOLD threshold by ~5 percentage points on upside
3. Wire TC market share into recommendation: if TC listing_share > 20%,
   increase HOLD bias (TC is price setter, not price taker at that share)

### Priority 2 — Improves model reliability
4. NCAA Basketball: either exclude from model scope explicitly or add
   game-importance features (rivalry flag, standings proximity to bubble)
   This requires external data not currently in the pipeline
5. Minimum comp quality gate: if n_comps < 5 AND confidence == LOW,
   output INSUFFICIENT_DATA rather than a directional recommendation
   Currently WEAK_COMP_CATEGORIES warning exists but still recommends
6. Run 2 more large backtests (12 events/category) after any changes
   before drawing conclusions — 6 runs is enough to see stable patterns
   but not enough to tune thresholds with confidence

### Priority 3 — Phase 2 scope
7. Section-level analysis using InventoryStream:
   - Query by EventID + SectionName for a target event
   - Compare GA vs reserved section pricing trajectories separately
   - Requires budget approval (23MB per EventID query, manageable)
   - This is explicitly called out in deliverable 1 and 4
8. ✅ Streamlit web app — COMPLETE (scripts/frontend.py)
9. GCP Cloud Run deployment with service account auth
10. ListingCount from VividIntake as supply signal in generate_recommendation
    (data already pulled in Step 1, just not passed to Step 10)

---

## Key Findings & Gotchas

### Data
- VividIntake partition 2026-04-03 contains ONLY future events
- InventoryStream has NO historical data before Jan 2025
- 75_Event_Daily is the correct source for ALL historical comp data
- 75_Event_Daily.PID = InventoryStream.EventID (same ID)
- listed_price (user input) anchors ALL predictions — market avg is context only
- Days before event calculated from actual event date, NOT last sale date
- Zach Bryan categorized as both "Country and Folk" and "Pop" —
  always use performer name lookup for concerts, not category filter
- Festival appearances have lower avg_order_size than headlining shows —
  always exclude festival events from concert baseline
- COVID-era events (2021) have suppressed prices — always exclude
- "Date TBD" events get placeholder dates (e.g. 2027-03-08) —
  be cautious with days_before_event calculations for these
- Always deduplicate on PID/EventID before scoring comps
- Women's events must be excluded when target is men's (and vice versa)
- Preseason games must be excluded for regular season comps
- Parking listings must always be filtered out
- Python 3.14 does NOT support pandas — use Python 3.12 via venv312

### Model behavior
- The tautological baseline problem: generate_recommendation re-anchors
  baseline at every call using current_price / current_index, meaning it
  can keep showing "upside" even as prices spiral down. prior_price breaks
  this loop — always pass baseline_price as prior_price in backtest
- Beat rate vs EV paradox: 74-84% beat rate vs hold-to-event with negative
  EV vs day-90 baseline is expected (negative skew distribution). Do not
  treat this as a model failure — it is the correct framing.
- Winsorized avg and median are more reliable than raw avg for evaluating
  typical performance — Taylor Swift type outliers (-$3000) make raw avg
  misleading even with 90 events
- Day-30 gate was tried and reverted — do not re-add without strong evidence
  from ≥3 large backtests showing it helps

### Environment
- Python 3.14 does NOT support pandas — use Python 3.12 via venv312
- Run backend: `python scripts/main.py`
- Run backtest: `python scripts/backtest.py`
- Run frontend: `streamlit run scripts/frontend.py`
- If packages missing: pip install pandas pyarrow db-dtypes
  google-cloud-bigquery numpy streamlit plotly

### Frontend (scripts/frontend.py)
- Built with Streamlit + Plotly
- Inputs: Event ID (required), Listed Price (optional), Cost Basis (optional)
- Default price when none given: `market['avg_order_size']` (recent actual sale avg)
  Previously defaulted to `MinPrice` (cheapest listing / get-in price) — changed
  because get-in anchors predictions unrealistically low vs actual sold prices
- Displays: recommendation banner, price forecast chart, market snapshot,
  comp table, sell-through pattern, TC market share, section analysis
- Market Snapshot shows "Data as of: {report_date}" for freshness visibility
- Price Forecast chart shows day0_p25_price as "Day-of Floor (p25)" marker
  = lowest 25th percentile outcome across comp events by event day
- Color system: Green=HOLD, Red=SELL_NOW, Amber=MONITOR
- Section analysis: classifies sections as DECAY_RISK / RELIABLE / BALANCED
  and GA vs RESERVED (GA if section name contains: GA, Standing, Lawn, SRO)
- Cache: @st.cache_data(ttl=1800) on run_pipeline() — 30 min in-memory

