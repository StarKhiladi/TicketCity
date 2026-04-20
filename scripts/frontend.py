import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from main import run_pipeline, PARTITION


@st.cache_data(show_spinner=False, ttl=1800)
def run_pipeline_cached(event_id: int, listed_price: float | None,
                        cost_basis: float | None, n_comps: int = 15):
    print(f"STREAMLIT CACHE MISS: run_pipeline_cached({event_id}, {listed_price}, {cost_basis}, {n_comps})")
    return run_pipeline(
        event_id=event_id,
        listed_price=listed_price,
        cost_basis=cost_basis,
        n_comps=n_comps,
    )

st.set_page_config(
    page_title="TicketCity Pricing Analyzer",
    page_icon="🎟️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Design Tokens (light theme) ────────────────────────────────────────────────
C_BG        = "#f1f5f9"   # page background
C_CARD      = "#ffffff"   # card surface
C_CARD2     = "#f8fafc"   # slightly tinted card / nested surface
C_BORDER    = "#e2e8f0"   # border
C_TEXT      = "#0f172a"   # primary text
C_MUTED     = "#64748b"   # secondary / label text
C_BLUE      = "#2563eb"   # accent / forecast line
C_GREEN     = "#16a34a"   # HOLD
C_AMBER     = "#d97706"   # MONITOR / MEDIUM risk
C_RED       = "#dc2626"   # SELL_NOW / HIGH risk
C_ORANGE    = "#ea580c"   # LAST_MINUTE_DEMAND
C_SLATE     = "#475569"   # INSUFFICIENT_DATA / neutral

# ── Global CSS ─────────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
  :root {{
      --bg: {C_BG};
      --card: {C_CARD};
      --card2: {C_CARD2};
      --border: {C_BORDER};
      --text: {C_TEXT};
      --muted: {C_MUTED};
      --blue: {C_BLUE};
      --green: {C_GREEN};
      --amber: {C_AMBER};
      --red: {C_RED};
      --orange: {C_ORANGE};
      --slate: {C_SLATE};
  }}

  /* App shell */
  html, body, [class*="css"] {{
      font-family: "Inter", "Segoe UI", system-ui, sans-serif;
      color: var(--text);
  }}

  .stApp,
  [data-testid="stAppViewContainer"],
  [data-testid="stMain"],
  .main,
  .main .block-container,
  section[data-testid="stSidebar"] {{
      background-color: var(--bg) !important;
      color: var(--text) !important;
  }}

  .main .block-container {{
      padding-top: 1.25rem !important;
      padding-bottom: 2rem !important;
  }}

  [data-testid="stHeader"] {{
      background-color: var(--card) !important;
      border-bottom: 1px solid var(--border) !important;
  }}

  /* Kill dark / transparent default containers */
  [data-testid="stVerticalBlock"] > div:has(> [data-testid="element-container"]),
  [data-testid="stHorizontalBlock"] > div {{
      background: transparent !important;
  }}

  /* Typography */
  h1, h2, h3, h4, h5, h6, p, span, div, label {{
      color: var(--text);
  }}

  /* Inputs wrapper */
  [data-testid="stTextInput"],
  [data-testid="stNumberInput"] {{
      background: var(--card2) !important;
      border: 1px solid var(--border) !important;
      border-radius: 10px !important;
      padding: 0.4rem 0.55rem !important;
  }}

  [data-testid="InputInstructions"] {{
    display: none !important;
}}

  /* Actual inputs */
  [data-testid="stTextInput"] input,
  [data-testid="stNumberInput"] input {{
      background: var(--card) !important;
      border: 1px solid var(--border) !important;
      border-radius: 8px !important;
      color: var(--text) !important;
      font-size: 0.95rem !important;
      caret-color: var(--blue) !important;
      box-shadow: none !important;
  }}
  /* Keep input hint text anchored correctly */
[data-testid="stTextInput"] > div,
[data-testid="stNumberInput"] > div {{
    position: relative;
}}

/* Fix "Press Enter to apply" placement */
[data-testid="stTextInput"] div[data-testid="InputInstructions"],
[data-testid="stNumberInput"] div[data-testid="InputInstructions"] {{
    position: absolute;
    right: 10px;
    bottom: 6px;
    font-size: 0.7rem;
    color: var(--muted) !important;
    pointer-events: none;
    white-space: nowrap;
}}
  [data-testid="stTextInput"] input:focus,
  [data-testid="stNumberInput"] input:focus {{
      border-color: var(--blue) !important;
      outline: none !important;
      box-shadow: 0 0 0 3px rgba(37,99,235,0.15) !important;
  }}

  /* Number input stepper buttons */
  [data-testid="stNumberInput"] button {{
      background: var(--card2) !important;
      border: 1px solid var(--border) !important;
      color: var(--text) !important;
      border-radius: 6px !important;
  }}

  [data-testid="stNumberInput"] button:hover {{
      background: var(--card) !important;
      border-color: var(--blue) !important;
      color: var(--blue) !important;
  }}

  /* Labels */
  label[data-testid="stWidgetLabel"] {{
      display: flex !important;
      align-items: center !important;
      gap: 6px !important;
      margin-bottom: 0.35rem !important;
  }}

  label[data-testid="stWidgetLabel"] > div > p {{
      color: var(--muted) !important;
      font-size: 0.78rem !important;
      font-weight: 700 !important;
      text-transform: uppercase !important;
      letter-spacing: 0.05em !important;
      margin: 0 !important;
  }}

  /* Help tooltip icon */
  [data-testid="stWidgetLabelHelp"] {{
      color: var(--muted) !important;
  }}

  [data-testid="stWidgetLabelHelp"]:hover {{
      color: var(--blue) !important;
  }}

  /* Tooltip content */
  [data-testid="stTooltipContent"] {{
      background-color: var(--card) !important;
      color: var(--text) !important;
      border: 1px solid var(--border) !important;
      border-radius: 8px !important;
      font-size: 0.8rem !important;
      padding: 8px 10px !important;
      box-shadow: 0 8px 24px rgba(15,23,42,0.10) !important;
  }}

  [data-testid="stTooltipContent"] * {{
      color: var(--text) !important;
      background: transparent !important;
  }}

  /* Buttons */
  [data-testid="stButton"] > button {{
      background: var(--blue) !important;
      color: #ffffff !important;
      border: none !important;
      border-radius: 8px !important;
      padding: 0.65rem 2.5rem !important;
      font-size: 0.95rem !important;
      font-weight: 700 !important;
      letter-spacing: 0.02em !important;
      transition: opacity 0.15s ease, transform 0.08s ease !important;
      box-shadow: 0 2px 8px rgba(37,99,235,0.20) !important;
  }}

  [data-testid="stButton"] > button:hover {{
      opacity: 0.9 !important;
  }}

  [data-testid="stButton"] > button:active {{
      transform: translateY(1px) !important;
  }}

  /* Divider */
  hr {{
      border-color: var(--border) !important;
      margin: 1.5rem 0 !important;
  }}

  /* Hide native metric widgets */
  [data-testid="stMetric"] {{
      display: none !important;
  }}

  /* Dataframe */
  [data-testid="stDataFrame"] {{
      border: 1px solid var(--border) !important;
      border-radius: 10px !important;
      overflow: hidden !important;
      background: var(--card) !important;
  }}

  [data-testid="stDataFrame"] * {{
      color: var(--text) !important;
  }}

  /* Spinner / status */
  [data-testid="stSpinner"] p {{
      color: var(--muted) !important;
  }}

  /* Section headers */
  .section-header {{
      font-size: 1.05rem;
      font-weight: 800;
      color: var(--text);
      margin: 0 0 0.8rem 0;
      padding-bottom: 0.4rem;
      border-bottom: 2px solid var(--blue);
      display: inline-block;
  }}

  /* Utility cards */
  .panel-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem 1.15rem;
      box-shadow: 0 1px 4px rgba(0,0,0,0.05);
  }}

  /* Animated loading bar */
  @keyframes shimmer {{
      0%   {{ transform: translateX(-100%); }}
      100% {{ transform: translateX(400%); }}
  }}
</style>
""", unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────────────────────────
def metric_card(label: str, value: str, delta: str = None,
                delta_good: bool = None, accent: str = None) -> str:
    border_top = f"border-top: 3px solid {accent};" if accent else ""
    delta_html = ""
    if delta:
        if delta_good is True:
            d_color, d_arrow = C_GREEN, "↑"
        elif delta_good is False:
            d_color, d_arrow = C_RED, "↓"
        else:
            d_color, d_arrow = C_MUTED, ""
        delta_html = (
            f'<div style="font-size:0.78rem;color:{d_color};'
            f'margin-top:5px;font-weight:700;">{d_arrow} {delta}</div>'
        )
    return f"""
    <div style="background:{C_CARD};border:1px solid {C_BORDER};border-radius:10px;
                padding:16px 18px;{border_top}
                box-shadow:0 1px 4px rgba(0,0,0,0.06);height:100%;">
      <div style="font-size:0.7rem;color:{C_MUTED};text-transform:uppercase;
                  letter-spacing:0.07em;font-weight:700;">{label}</div>
      <div style="font-size:1.45rem;font-weight:800;color:{C_TEXT};
                  margin-top:5px;line-height:1.1;">{value}</div>
      {delta_html}
    </div>
    """


def metrics_row(cards: list[tuple]) -> None:
    cols = st.columns(len(cards))
    for col, card_args in zip(cols, cards):
        with col:
            st.markdown(metric_card(*card_args), unsafe_allow_html=True)


def section_header(title: str) -> None:
    st.markdown(f'<div class="section-header">{title}</div>', unsafe_allow_html=True)


def panel_open():
    st.markdown('<div class="panel-card">', unsafe_allow_html=True)


def panel_close():
    st.markdown('</div>', unsafe_allow_html=True)


# ── Page Header ────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="background:{C_CARD};border:1px solid {C_BORDER};
            border-radius:14px;padding:1.1rem 1.4rem;
            display:flex;align-items:center;gap:0.8rem;
            box-shadow:0 1px 4px rgba(0,0,0,0.05);">
  <span style="font-size:1.6rem;">🎟️</span>
  <div>
    <div style="font-size:1.42rem;font-weight:800;color:{C_TEXT};
                letter-spacing:-0.02em;">TicketCity Pricing Analyzer</div>
    <div style="font-size:0.82rem;color:{C_MUTED};margin-top:2px;
                display:flex;align-items:center;gap:0.6rem;">
      Data-driven sell / hold recommendations for ticket inventory
      <span style="display:inline-flex;align-items:center;gap:4px;
                   background:{C_BG};border:1px solid {C_BORDER};
                   border-radius:6px;padding:1px 8px;
                   font-size:0.75rem;color:{C_MUTED};font-weight:600;">
        🗓 Data as of {PARTITION}
      </span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

st.markdown("<div style='height:1.1rem;'></div>", unsafe_allow_html=True)

# ── Inputs ─────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="panel-card" style="padding:1.35rem 1.5rem 1rem;">
  <div style="font-size:0.95rem;font-weight:800;color:{C_TEXT};
              margin-bottom:0.1rem;">Run Analysis</div>
  <div style="font-size:0.8rem;color:{C_MUTED};margin-bottom:1rem;">
    Enter an Event ID and optionally your listed price and cost basis.
  </div>
</div>
""", unsafe_allow_html=True)

with st.container():
    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])

    with col1:
        event_id_raw = st.text_input(
            "Event ID",
            placeholder="e.g. 5964892",
            help="The TicketCity / Vivid event identifier you want to analyze."
        )

    with col2:
        listed_price = st.number_input(
            "Listed Price per Ticket",
            min_value=0.0,
            value=0.0,
            format="%.2f",
            help="Your current ask price. Anchors all predictions."
        )

    with col3:
        cost_basis = st.number_input(
            "Cost Basis per Ticket",
            min_value=0.0,
            value=0.0,
            format="%.2f",
            help="What you paid. Used for P&L calculations."
        )

    with col4:
        st.markdown("<div style='height:1.92rem;'></div>", unsafe_allow_html=True)
        run_btn = st.button("Run Analysis", use_container_width=True)

st.divider()

# ── Analysis ───────────────────────────────────────────────────────────────────
if run_btn:
    if not event_id_raw or not event_id_raw.strip().isdigit():
        st.markdown(f"""
        <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:10px;
                    padding:1rem 1.25rem;color:#991b1b;font-weight:700;">
          ⚠️ Please enter a valid numeric Event ID.
        </div>
        """, unsafe_allow_html=True)
        st.stop()

    _loading = st.empty()
    _steps = [
        "Fetching event info from BigQuery",
        "Identifying comparable events",
        "Pulling daily price trajectories",
        "Normalizing & aggregating curves",
        "Computing sell-through metrics",
        "Generating recommendation",
    ]
    _steps_html = "".join(
        f'<span style="color:{C_MUTED};font-size:0.78rem;padding:2px 8px;'
        f'border-radius:5px;background:{C_CARD2};border:1px solid {C_BORDER};">{s}</span>'
        for s in _steps
    )
    _loading.markdown(f"""
    <div style="background:{C_CARD};border:1px solid {C_BORDER};border-radius:12px;
                padding:1.75rem 2rem;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,0.05);">
      <div style="font-size:1.05rem;font-weight:800;color:{C_TEXT};margin-bottom:0.35rem;">
        Analyzing Event {event_id_raw.strip()}…
      </div>
      <div style="font-size:0.82rem;color:{C_MUTED};margin-bottom:1.25rem;">
        Querying BigQuery · Building comp trajectories · Generating recommendation
      </div>
      <div style="background:{C_BORDER};border-radius:6px;height:5px;
                  overflow:hidden;position:relative;max-width:480px;margin:0 auto 1.25rem;">
        <div style="position:absolute;top:0;left:0;height:100%;width:35%;
                    background:linear-gradient(90deg,{C_BLUE}66,{C_BLUE},{C_BLUE}66);
                    border-radius:6px;
                    animation:shimmer 1.6s ease-in-out infinite;"></div>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;justify-content:center;">
        {_steps_html}
      </div>
    </div>
    """, unsafe_allow_html=True)

    result = run_pipeline_cached(
    event_id=int(event_id_raw.strip()),
    listed_price=listed_price or None,
    cost_basis=cost_basis or None,
    n_comps=15,
)
    _loading.empty()

    if not result:
        st.markdown(f"""
        <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:10px;
                    padding:1rem 1.25rem;color:#991b1b;font-weight:700;">
          ❌ No results found for Event ID {event_id_raw.strip()}. Please verify and try again.
        </div>
        """, unsafe_allow_html=True)
        st.stop()

    rec        = result['rec']
    info       = result['info']
    market     = result['market']
    comps      = result['comps']
    confidence = result['confidence']
    tc_share   = result['tc_share']
    demand     = result['demand']
    st_summary = result['st_summary']
    decay      = result.get('decay')
    section    = result.get('section')

    recommendation = rec['recommendation']

    # ── Event Header ──────────────────────────────────────────────────────────
    st.markdown(f"""
    <div class="panel-card" style="margin-bottom:1rem;">
      <div style="font-size:1.55rem;font-weight:800;color:{C_TEXT};
                  letter-spacing:-0.02em;">{info['Name']}</div>
      <div style="font-size:0.875rem;color:{C_MUTED};margin-top:0.3rem;">
        📍 {info['Venue_Name']}, {info['City']}, {info['State']}
        &nbsp;·&nbsp; 📅 {info['LocalDate']}
        &nbsp;·&nbsp; 🏷️ {info['Category_Name']}
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Recommendation Banner ─────────────────────────────────────────────────
    REC_STYLES = {
        'SELL_NOW':          (C_RED,   "#fef2f2", "🔴", "SELL NOW"),
        'HOLD':              (C_GREEN, "#f0fdf4", "🟢", "HOLD"),
        'MONITOR':           (C_AMBER, "#fffbeb", "🟡", "MONITOR"),
        'INSUFFICIENT_DATA': (C_SLATE, "#f8fafc", "⚪", "INSUFFICIENT DATA"),
    }
    r_color, r_bg, r_icon, r_label = REC_STYLES.get(
        recommendation, (C_SLATE, "#f8fafc", "⚪", recommendation)
    )
    st.markdown(f"""
    <div style="background:{r_bg};border:1px solid {r_color};border-left:5px solid {r_color};
                border-radius:10px;padding:1rem 1.5rem;margin-bottom:1rem;">
      <div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.4rem;">
        <span style="font-size:1.1rem;">{r_icon}</span>
        <span style="font-size:1rem;font-weight:800;color:{r_color};
                     letter-spacing:0.03em;">RECOMMENDATION: {r_label}</span>
      </div>
      <div style="font-size:0.9rem;color:{C_TEXT};line-height:1.55;">
        {rec['reasoning']}
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Category Risk ─────────────────────────────────────────────────────────
    CATEGORY_RISK = {
        'Comedy':          ('HIGH',        C_RED,   "#fef2f2", "Comp pools unreliable — trajectory highly variable"),
        'NCAA Basketball': ('HIGH',        C_RED,   "#fef2f2", "Demand spikes near game day not captured by comps"),
        'Rock':            ('MEDIUM-HIGH', C_ORANGE, "#fff7ed", "Artist-specific demand not reflected in genre comps"),
        'Pop':             ('MEDIUM-HIGH', C_ORANGE, "#fff7ed", "Outlier events skew comp trajectories"),
        'MLB Baseball':    ('MEDIUM',      C_AMBER, "#fffbeb", "Flat decay curves — low comp differentiation"),
    }
    cat_risk = CATEGORY_RISK.get(info['Category_Name'])
    if cat_risk:
        cr_level, cr_color, cr_bg, cr_desc = cat_risk
        st.markdown(f"""
        <div style="background:{cr_bg};border:1px solid {cr_color};border-left:4px solid {cr_color};
                    border-radius:8px;padding:0.7rem 1rem;margin-bottom:1rem;
                    font-size:0.85rem;color:{C_TEXT};">
          ⚠️ <strong>Category Risk ({cr_level}):</strong> {cr_desc}
        </div>
        """, unsafe_allow_html=True)

    # ── Key Metrics Row ───────────────────────────────────────────────────────
    realized_decline = rec.get('realized_decline_pct', 0.0)

    conf_color = {
        'HIGH': C_GREEN,
        'MEDIUM': C_AMBER,
        'LOW': C_RED,
    }.get(confidence, C_MUTED)

    metrics_row([
        ("Current Price", f"${rec['current_price']:,.2f}", None, None, C_BLUE),
        ("Baseline (Day-90)", f"${rec['baseline_price']:,.2f}",
         f"{realized_decline:+.1f}% realized" if realized_decline else None,
         realized_decline >= 0 if realized_decline else None,
         C_SLATE),
        ("Peak Forecast", f"${rec['peak_price']:,.2f}",
         f"{rec['upside_pct']:+.1f}% upside",
         rec['upside_pct'] > 0, C_GREEN),
        ("Day-Of Forecast", f"${rec['day0_price']:,.2f}",
         f"{rec['day0_pct']:+.1f}% median · floor ${rec.get('day0_p25_price', rec['day0_price']):,.0f} ({rec.get('day0_p25_pct', rec['day0_pct']):+.1f}%)",
         rec['day0_pct'] > 0, C_AMBER),
        ("Days to Event", str(rec['days_remaining']), None, None, C_BLUE),
        ("Comp Confidence", confidence, None, None, conf_color),
    ])

    st.divider()

    # ── Price Forecast Chart ──────────────────────────────────────────────────
    section_header("Price Forecast")

    predictions = rec.get('predictions', {})
    if predictions:
        # Build prediction dataframe from model output
        df_pred = pd.DataFrame([
            {
                "Days Before Event": day,
                "Predicted Price": data["predicted_price"],
                "Low Estimate": data["low_estimate"],
                "High Estimate": data["high_estimate"],
                "n_events": data.get("n_events", ""),
            }
            for day, data in sorted(predictions.items(), reverse=True)
        ])

        days_rem       = rec['days_remaining']
        cur_price      = rec['current_price']
        base_price     = rec['baseline_price']
        peak_price     = rec['peak_price']
        day0_price     = rec['day0_price']
        day0_p25_price = rec.get('day0_p25_price', day0_price)
        day0_p25_pct   = rec.get('day0_p25_pct', 0)
        peak_day       = rec.get('peak_day', 0)

        # Add the current point explicitly
        current_row = pd.DataFrame([{
            "Days Before Event": days_rem,
            "Predicted Price": cur_price,
            "Low Estimate": cur_price,
            "High Estimate": cur_price,
            "n_events": "",
        }])

        df_pred = (
            pd.concat([current_row, df_pred], ignore_index=True)
            .drop_duplicates(subset=["Days Before Event"], keep="first")
            .sort_values("Days Before Event", ascending=False)
            .reset_index(drop=True)
        )

        # Convert axis so chart starts at today:
        # 0 = today, positive numbers = days from today until event
        df_pred["Days From Today"] = days_rem - df_pred["Days Before Event"]

        # Historical = before today, Forecast = today forward
        df_future = df_pred[df_pred["Days From Today"] >= 0].copy()
        df_past   = df_pred[df_pred["Days From Today"] < 0].copy()

        traj_color = C_GREEN if day0_price >= cur_price * 0.97 else C_RED
        day0_marker_color = C_RED if day0_price < cur_price * 0.95 else C_GREEN

        fig = go.Figure()

        # Highlight today
        fig.add_vrect(
            x0=-0.5, x1=0.5,
            fillcolor=C_BLUE, opacity=0.12,
            layer="below", line_width=0,
        )

        # Forecast confidence band
        if not df_future.empty:
            band_x = list(df_future["Days From Today"]) + list(df_future["Days From Today"][::-1])
            band_y = list(df_future["High Estimate"]) + list(df_future["Low Estimate"][::-1])
            fig.add_trace(go.Scatter(
                x=band_x,
                y=band_y,
                fill='toself',
                fillcolor='rgba(37,99,235,0.08)',
                line=dict(color='rgba(0,0,0,0)'),
                name='Forecast Range',
                hoverinfo='skip',
                showlegend=True,
            ))

        # Baseline reference line
        fig.add_hline(
            y=base_price,
            line_dash="dash",
            line_color=C_SLATE,
            line_width=1.2,
        )

        # Historical trend (only if you actually have points before today)
        if not df_past.empty:
            fig.add_trace(go.Scatter(
                x=df_past["Days From Today"],
                y=df_past["Predicted Price"],
                mode='lines',
                name='Historical Trend',
                line=dict(color=C_SLATE, width=2, dash='dot'),
                hovertemplate="<b>%{x} days from today</b><br>$%{y:,.2f}<extra>Historical</extra>",
                showlegend=True,
            ))

        # Forecast line from today forward
        if not df_future.empty:
            fig.add_trace(go.Scatter(
                x=df_future["Days From Today"],
                y=df_future["Predicted Price"],
                mode='lines',
                name='Price Forecast',
                line=dict(color=traj_color, width=3),
                hovertemplate="<b>%{x} days from today</b><br>$%{y:,.2f}<extra>Forecast</extra>",
                showlegend=True,
            ))

        # Current point at today = 0
        fig.add_trace(go.Scatter(
            x=[0],
            y=[cur_price],
            mode='markers',
            name='Your Price (now)',
            marker=dict(
                size=13,
                color=C_BLUE,
                line=dict(color=C_CARD, width=2),
                symbol='circle'
            ),
            hovertemplate=f"<b>Today</b><br>${cur_price:,.2f}<extra>Your Price</extra>",
            showlegend=True,
        ))

        # Peak point
        if peak_day is not None and peak_day >= 0 and abs(peak_price - cur_price) > 0.01:
            peak_x = days_rem - peak_day
            fig.add_trace(go.Scatter(
                x=[peak_x],
                y=[peak_price],
                mode='markers',
                name='Forecast Peak',
                marker=dict(
                    size=11,
                    color=C_GREEN,
                    symbol='star',
                    line=dict(color=C_CARD, width=1.5)
                ),
                hovertemplate=f"<b>Peak ({peak_x} days from today)</b><br>${peak_price:,.2f}<extra>Peak</extra>",
                showlegend=True,
            ))

        # Event-day point (median forecast)
        fig.add_trace(go.Scatter(
            x=[days_rem],
            y=[day0_price],
            mode='markers',
            name='Day-of Forecast',
            marker=dict(
                size=10,
                color=day0_marker_color,
                symbol='diamond',
                line=dict(color=C_CARD, width=1.5)
            ),
            hovertemplate=f"<b>Event Day ({days_rem} days from today)</b><br>${day0_price:,.2f}<extra>Day-of Median</extra>",
            showlegend=True,
        ))

        # Day-of floor: p25 of comp outcomes at event day.
        # 1-in-4 comparable events ended at or below this price —
        # represents unsmoothed last-minute risk TFS daily avgs don't capture.
        if day0_p25_price < day0_price * 0.99:
            fig.add_trace(go.Scatter(
                x=[days_rem],
                y=[day0_p25_price],
                mode='markers',
                name='Day-of Floor (p25)',
                marker=dict(
                    size=9,
                    color=C_RED,
                    symbol='triangle-down',
                    line=dict(color=C_CARD, width=1.5),
                    opacity=0.85,
                ),
                hovertemplate=(
                    f"<b>Day-of Floor p25 ({days_rem} days from today)</b><br>"
                    f"${day0_p25_price:,.2f} ({day0_p25_pct:+.1f}% vs now)<br>"
                    f"1-in-4 comps ended at or below this price"
                    f"<extra>Floor Risk</extra>"
                ),
                showlegend=True,
            ))

        # Annotations
        ann_points = [
            (0, cur_price, f"Now ${cur_price:,.2f}", C_BLUE),
            (days_rem, day0_price, f"Day-0 ${day0_price:,.2f}", day0_marker_color),
        ]

        if peak_day is not None and peak_day >= 0 and abs(peak_price - cur_price) > 0.01:
            ann_points.append((days_rem - peak_day, peak_price, f"Peak ${peak_price:,.2f}", C_GREEN))

        ann_points.sort(key=lambda t: t[1], reverse=True)
        base_offsets = [-35, -65, -95]

        for i, (ax, ay, alabel, acolor) in enumerate(ann_points[:3]):
            fig.add_annotation(
                x=ax,
                y=ay,
                text=f"<b>{alabel}</b>",
                showarrow=True,
                arrowhead=2,
                arrowsize=0.8,
                arrowwidth=1.2,
                arrowcolor=acolor,
                ax=0,
                ay=base_offsets[i],
                font=dict(color=acolor, size=10.5, family="Inter, sans-serif"),
                bgcolor=C_CARD,
                bordercolor=acolor,
                borderwidth=1,
                borderpad=4,
                opacity=0.95,
            )

        # Baseline label — right-anchored so it never collides with the "Now"
        # annotation box which is always at x=0 (left edge).
        fig.add_annotation(
            x=0.99,
            xref="paper",
            y=0.02,
            yref="paper",
            text=f"Baseline ${base_price:,.2f}",
            showarrow=False,
            font=dict(color=C_MUTED, size=10, family="Inter, sans-serif"),
            xanchor="right",
            yanchor="bottom",
            bgcolor="rgba(0,0,0,0)",
        )

        # Y-axis bounds
        all_prices = (
            list(df_pred["High Estimate"]) +
            list(df_pred["Low Estimate"]) +
            [base_price, cur_price, peak_price, day0_price, day0_p25_price]
        )
        positive_prices = [p for p in all_prices if p > 0]
        price_min = min(positive_prices) if positive_prices else 0
        price_max = max(all_prices) if all_prices else 0
        padding = (price_max - price_min) * 0.18 if price_max > price_min else 20

        fig.update_layout(
            xaxis=dict(
                autorange=True,
                title=dict(text="Days From Today", font=dict(color=C_MUTED, size=12)),
                showgrid=True,
                gridcolor="rgba(226,232,240,0.7)",
                tickfont=dict(color=C_MUTED, size=11),
                zeroline=True,
                zerolinecolor=C_BLUE,
                zerolinewidth=2,
                tickmode="auto",
                nticks=10,
                range=[-1, days_rem + 1],
            ),
            yaxis=dict(
                title=dict(text="Price (USD)", font=dict(color=C_MUTED, size=12)),
                tickprefix="$",
                tickformat=",.0f",
                showgrid=True,
                gridcolor="rgba(226,232,240,0.7)",
                tickfont=dict(color=C_MUTED, size=11),
                zeroline=False,
                range=[max(0, price_min - padding), price_max + padding],
            ),
            plot_bgcolor=C_CARD,
            paper_bgcolor=C_CARD,
            hovermode="x unified",
            hoverlabel=dict(
                bgcolor=C_CARD,
                bordercolor=C_BORDER,
                font=dict(color=C_TEXT, size=12),
            ),
            legend=dict(
                orientation="h",
                y=-0.18,
                x=0,
                font=dict(color=C_MUTED, size=11),
                bgcolor="rgba(0,0,0,0)",
            ),
            height=440,
            margin=dict(t=40, b=70, l=10, r=20),
            font=dict(family="Inter, sans-serif", color=C_MUTED),
            shapes=[dict(
                type="rect",
                xref="paper",
                yref="paper",
                x0=0, y0=0, x1=1, y1=1,
                line=dict(color=C_BORDER, width=1),
                fillcolor="rgba(0,0,0,0)",
            )],
        )

        upside_str = f"{rec['upside_pct']:+.1f}% to peak" if rec['upside_pct'] else ""
        day0_str   = f"{rec['day0_pct']:+.1f}% by event day" if rec['day0_pct'] else ""
        caption = "  ·  ".join(filter(None, [upside_str, day0_str]))

        if caption:
            st.markdown(
                f'<div style="font-size:0.78rem;color:{C_MUTED};margin-top:-0.5rem;'
                f'margin-bottom:0.5rem;padding-left:4px;">'
                f'Based on {len(df_pred)} plotted points from {len(predictions)} forecast horizons'
                f'&ensp;·&ensp;{caption}</div>',
                unsafe_allow_html=True,
            )

        st.plotly_chart(fig, use_container_width=True)

    else:
        st.markdown(f"""
        <div style="background:{C_CARD};border:1px solid {C_BORDER};border-radius:8px;
                    padding:1rem;color:{C_MUTED};text-align:center;font-size:0.9rem;">
        Insufficient comp data to generate a price forecast.
        </div>
        """, unsafe_allow_html=True)

    st.divider()

    # ── Sell-Through | Demand | Market Snapshot ───────────────────────────────
    left, right = st.columns([1, 1])

    with left:
        section_header("Sell-Through Pattern")
        if st_summary:
            fig_pie = go.Figure(go.Pie(
                labels=["Early (60+ days)", "Mid (14–60 days)", "Late (0–14 days)"],
                values=[
                    st_summary['median_early_pct'],
                    st_summary['median_mid_pct'],
                    st_summary['median_late_pct'],
                ],
                marker_colors=[C_BLUE, C_AMBER, C_RED],
                hole=0.5,
                textfont=dict(size=12, color="#ffffff"),
                hovertemplate="%{label}: %{value:.1f}%<extra></extra>",
            ))
            fig_pie.update_layout(
                height=280,
                margin=dict(t=10, b=10, l=0, r=0),
                plot_bgcolor=C_CARD,
                paper_bgcolor=C_CARD,
                legend=dict(orientation="h", y=-0.15, font=dict(color=C_MUTED, size=11)),
                font=dict(family="Inter, sans-serif", color=C_TEXT),
            )
            st.plotly_chart(fig_pie, use_container_width=True)
            peak_day = st_summary['typical_peak_day']
            st.markdown(
                f'<div style="font-size:0.82rem;color:{C_MUTED};text-align:center;">'
                f'Peak demand at <strong style="color:{C_TEXT};">{peak_day:.0f} days</strong>'
                f' before event</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div style="color:{C_MUTED};font-size:0.9rem;">No sell-through data available.</div>',
                unsafe_allow_html=True
            )
    with right:
        section_header("Demand Signal")
        if demand:
            sig = demand['signal']
            sig_color = {
                'HIGH': C_GREEN,
                'MEDIUM': C_AMBER,
                'LOW': C_RED,
                'UNKNOWN': C_MUTED,
            }.get(sig, C_MUTED)
            sig_bg = {
                'HIGH': "#f0fdf4",
                'MEDIUM': "#fffbeb",
                'LOW': "#fef2f2",
                'UNKNOWN': "#f8fafc",
            }.get(sig, "#f8fafc")

            st.markdown(
                f"""<div style="background:{sig_bg};border:1px solid {sig_color};border-radius:8px;
padding:0.75rem 1rem;margin-bottom:0.75rem;">
<span style="font-size:0.72rem;color:{C_MUTED};font-weight:700;
text-transform:uppercase;letter-spacing:0.07em;">
Current Demand
</span>
<div style="font-size:1.3rem;font-weight:800;color:{sig_color};margin-top:3px;">
{sig.title()}
</div>
<div style="font-size:0.82rem;color:{C_TEXT};margin-top:0.4rem;line-height:1.45;">
{demand['explanation']}
</div>
</div>""",
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                f'<div style="color:{C_MUTED};font-size:0.9rem;">No demand signal data.</div>',
                unsafe_allow_html=True
            )

        # Market Snapshot
        st.markdown("<div style='height:0.5rem;'></div>", unsafe_allow_html=True)
        section_header("Market Snapshot")

        if not info:
            st.markdown(
                f'<div style="color:{C_MUTED};font-size:0.9rem;">No market snapshot data available.</div>',
                unsafe_allow_html=True
            )
        else:
            rows = [
                ("Listing Range", f'${info["MinPrice"]:,.2f} – ${info["MaxPrice"]:,.2f}'),
                ("Active Listings", f'{info["ListingCount"]:,}')
            ]

            if market:
                rows.extend([
                    ("Most Recent Sale", f'${market["avg_order_size"]:,.2f}'),
                    ("Orders on That Date", f'{market["orders"]:,}'),
                    ("Data as of", str(market.get("report_date", "—"))),
                ])

            rows_html = "".join(
                f'<tr>'
                f'<td style="color:{C_MUTED};font-size:0.88rem;padding:0.42rem 0;">{label}</td>'
                f'<td style="color:{C_TEXT};font-size:0.9rem;font-weight:700;text-align:right;padding:0.42rem 0;">{value}</td>'
                f'</tr>'
                for label, value in rows
            )

            snapshot_html = (
                f'<div style="background:{C_CARD};border:1px solid {C_BORDER};border-radius:10px;'
                f'padding:0.9rem 1rem;box-shadow:0 1px 3px rgba(0,0,0,0.04);">'
                f'<table style="width:100%;border-collapse:collapse;">'
                f'{rows_html}'
                f'</table>'
                f'</div>'
            )

            st.markdown(snapshot_html, unsafe_allow_html=True)

    # ── Decay Profile ──────   ───────────────────────────────────────────────────
    if decay and decay.get('decay_class', 'UNKNOWN') != 'UNKNOWN':
        section_header("Decay Profile")
        DECAY_STYLES = {
            'RELIABLE':   (C_GREEN, "#f0fdf4"),
            'MODERATE':   (C_AMBER, "#fffbeb"),
            'HIGH_DECAY': (C_RED, "#fef2f2"),
        }
        FLAG_ICONS = {
            'SELL_EARLY_REQUIRED': ('🔴', C_RED),
            'HOLD_THROUGH_PEAK':   ('🟢', C_GREEN),
            'SELL_ANYTIME':        ('🟡', C_AMBER),
            'LAST_MINUTE_DEMAND':  ('🟠', C_ORANGE),
        }
        d_color, d_bg = DECAY_STYLES.get(decay['decay_class'], (C_SLATE, "#f8fafc"))
        f_icon, f_color = FLAG_ICONS.get(decay['timing_flag'], ('⚪', C_MUTED))
        st.markdown(f"""
        <div style="background:{d_bg};border:1px solid {d_color};border-left:5px solid {d_color};
                    border-radius:10px;padding:1rem 1.25rem;margin-bottom:1rem;">
          <div style="display:flex;gap:1.5rem;align-items:baseline;margin-bottom:0.4rem;">
            <span style="font-size:0.9rem;font-weight:800;color:{d_color};
                         letter-spacing:0.03em;">{decay['decay_class']}</span>
            <span style="font-size:0.85rem;font-weight:700;color:{f_color};">
              {f_icon} {decay['timing_flag']}
            </span>
          </div>
          <div style="font-size:0.875rem;color:{C_TEXT};line-height:1.55;">
            {decay['guidance']}
          </div>
        </div>
        """, unsafe_allow_html=True)

        metrics_row([
            ("Median Decay to Day-0", f"{decay.get('med_decay_pct', 0):+.1f}%", None, None, d_color),
            ("Median Peak Uplift", f"{decay.get('med_peak_uplift', 0):+.1f}%", None, None, C_GREEN),
            ("Early Sales (60+ days)", f"{decay.get('med_early_pct', 0):.0f}%", None, None, C_BLUE),
            ("Late Sales (0–14 days)", f"{decay.get('med_late_pct', 0):.0f}%", None, None, C_AMBER),
        ])
        st.divider()

    # ── P&L Summary ───────────────────────────────────────────────────────────
    if 'cost_basis' in rec:
        section_header("Profit & Loss Summary")
        pnl_cards = [
            ("Sell Now", f"${rec['current_price']:,.2f}",
             f"{rec['current_profit_pct']:+.1f}%  (${rec['current_profit_abs']:+.2f})",
             rec['current_profit_pct'] >= 0, C_BLUE),
            ("Sell at Peak", f"${rec['peak_price']:,.2f}",
             f"{rec['peak_profit_pct']:+.1f}%  (${rec['peak_profit_abs']:+.2f})",
             rec['peak_profit_pct'] >= 0, C_GREEN),
            ("Wait Until Day-Of", f"${rec['day0_price']:,.2f}",
             f"{rec['day0_profit_pct']:+.1f}%  (${rec['day0_profit_abs']:+.2f})"
             + (f"  ·  floor ${rec.get('day0_p25_price', rec['day0_price']):,.0f} p25"
                if rec.get('day0_p25_price', rec['day0_price']) < rec['day0_price'] * 0.99 else ""),
             rec['day0_profit_pct'] >= 0, C_AMBER),
        ]
        metrics_row(pnl_cards)
        st.divider()

    # ── TicketCity Market Position ────────────────────────────────────────────
    if tc_share and tc_share['tc_blocks'] > 0:
        section_header("TicketCity Market Position")
        metrics_row([
            ("TC Listing Blocks", str(tc_share['tc_blocks']), None, None, C_BLUE),
            ("Listing Share", f"{tc_share['listing_share']}%", None, None, C_BLUE),
            ("Ticket Share", f"{tc_share['ticket_share']}%", None, None, C_BLUE),
        ])
        st.markdown("<div style='height:0.75rem;'></div>", unsafe_allow_html=True)

        tc_vals = [
            tc_share['tc_blocks'],
            max(tc_share['total_listings'] - tc_share['tc_blocks'], 0)
        ]
        fig_bar = go.Figure(go.Bar(
            x=["TicketCity", "Rest of Market"],
            y=tc_vals,
            marker_color=[C_BLUE, C_BORDER],
            text=[f"{v:,}" for v in tc_vals],
            textposition="outside",
            textfont=dict(color=C_TEXT, size=12),
        ))
        fig_bar.update_layout(
            height=240,
            showlegend=False,
            plot_bgcolor=C_CARD,
            paper_bgcolor=C_CARD,
            margin=dict(t=10, b=10, l=10, r=10),
            yaxis=dict(
                showgrid=True, gridcolor="#f1f5f9",
                tickfont=dict(color=C_MUTED),
                showticklabels=False
            ),
            xaxis=dict(
                tickfont=dict(color=C_TEXT, size=13, family="Inter, sans-serif")
            ),
            font=dict(family="Inter, sans-serif", color=C_TEXT),
        )
        st.plotly_chart(fig_bar, use_container_width=True)
        st.divider()

    # ── Section Analysis ──────────────────────────────────────────────────────
    if section and section.get('snapshot_days', 0) > 0:
        section_header("Section & Market Dynamics")
        st.markdown(
            f'<div style="font-size:0.8rem;color:{C_MUTED};margin-bottom:0.75rem;">'
            f'{section["snapshot_days"]} snapshot days &nbsp;·&nbsp; '
            f'{section["source_count"]} marketplace sources &nbsp;·&nbsp; '
            f'{section.get("total_listings", 0):,} total listings</div>',
            unsafe_allow_html=True,
        )

        sec_left, sec_right = st.columns(2)

        with sec_left:
            ga_tbl = section.get('ga_vs_reserved')
            if isinstance(ga_tbl, pd.DataFrame) and not ga_tbl.empty:
                st.markdown(
                    f'<div style="font-size:0.82rem;font-weight:800;color:{C_TEXT};margin-bottom:0.3rem;">GA vs Reserved</div>',
                    unsafe_allow_html=True
                )
                st.dataframe(
                    ga_tbl[['segment', 'n_listings', 'total_tickets', 'median_price']]
                    .rename(columns={
                        'segment': 'Segment',
                        'n_listings': 'Listings',
                        'total_tickets': 'Tickets',
                        'median_price': 'Median $'
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

            scarcity = section.get('scarcity_leaders')
            if isinstance(scarcity, pd.DataFrame) and not scarcity.empty:
                st.markdown(
                    f'<div style="font-size:0.82rem;font-weight:800;color:{C_TEXT};margin:0.75rem 0 0.3rem;">Tightest Supply Sections</div>',
                    unsafe_allow_html=True
                )
                st.dataframe(
                    scarcity[['section_group', 'ticket_class',
                              'latest_ticket_count', 'latest_median_price', 'behavior_flag']]
                    .head(5)
                    .rename(columns={
                        'section_group': 'Section',
                        'ticket_class': 'Class',
                        'latest_ticket_count': 'Tickets',
                        'latest_median_price': 'Median $',
                        'behavior_flag': 'Flag'
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

        with sec_right:
            tc_sec = section.get('tc_position_latest')
            if isinstance(tc_sec, pd.DataFrame) and not tc_sec.empty:
                st.markdown(
                    f'<div style="font-size:0.82rem;font-weight:800;color:{C_TEXT};margin-bottom:0.3rem;">TicketCity Share by Section</div>',
                    unsafe_allow_html=True
                )
                st.dataframe(
                    tc_sec[['section_group', 'ticket_class',
                           'tc_ticket_share', 'tc_listing_share']]
                    .head(5)
                    .rename(columns={
                        'section_group': 'Section',
                        'ticket_class': 'Class',
                        'tc_ticket_share': 'TC Ticket %',
                        'tc_listing_share': 'TC Listing %'
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

            source_tbl = section.get('source_summary')
            if isinstance(source_tbl, pd.DataFrame) and not source_tbl.empty:
                latest_date = source_tbl['snapshot_date'].max()
                latest_sources = (
                    source_tbl[source_tbl['snapshot_date'] == latest_date]
                    .sort_values('listing_count', ascending=False)
                )
                st.markdown(
                    f'<div style="font-size:0.82rem;font-weight:800;color:{C_TEXT};margin:0.75rem 0 0.3rem;">Marketplace Breakdown</div>',
                    unsafe_allow_html=True
                )
                st.dataframe(
                    latest_sources[['DataSource', 'listing_count', 'ticket_count', 'median_price']]
                    .head(5)
                    .rename(columns={
                        'DataSource': 'Source',
                        'listing_count': 'Listings',
                        'ticket_count': 'Tickets',
                        'median_price': 'Median $'
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

        decay_sec = section.get('decay_sections')
        if isinstance(decay_sec, pd.DataFrame) and not decay_sec.empty:
            st.markdown(
                f'<div style="font-size:0.82rem;font-weight:800;color:{C_TEXT};margin:0.75rem 0 0.3rem;">Highest Decay-Risk Sections</div>',
                unsafe_allow_html=True
            )
            st.dataframe(
                decay_sec[['section_group', 'ticket_class',
                          'price_change_14d_pct', 'latest_ticket_count']]
                .head(5)
                .rename(columns={
                    'section_group': 'Section',
                    'ticket_class': 'Class',
                    'price_change_14d_pct': '14d Price Δ%',
                    'latest_ticket_count': 'Tickets'
                }),
                use_container_width=True,
                hide_index=True,
            )
        st.divider()

    # ── Comparable Events ─────────────────────────────────────────────────────
    section_header("Comparable Events")
    if not comps.empty:
        display_cols = ['tier', 'weight', 'Name', 'EventDate', 'City',
                        'total_orders', 'avg_order_size', 'similarity_score']
        display_cols = [c for c in display_cols if c in comps.columns]
        df_display = comps[display_cols].head(15).copy()
        rename_map = {
            'tier': 'T', 'weight': 'Wt',
            'total_orders': 'Orders', 'avg_order_size': 'Avg Price',
            'similarity_score': 'Score',
            'EventDate': 'Date', 'Name': 'Event',
        }
        df_display.columns = [rename_map.get(c, c.replace('_', ' ').title())
                               for c in df_display.columns]
        st.dataframe(df_display, use_container_width=True, hide_index=True)
    else:
        st.markdown(
            f'<div style="color:{C_MUTED};font-size:0.9rem;">No comparable events found.</div>',
            unsafe_allow_html=True
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    st.markdown(f"""
    <div style="margin-top:2rem;padding-top:1rem;border-top:1px solid {C_BORDER};
                font-size:0.75rem;color:{C_MUTED};text-align:center;">
      TicketCity Pricing Analyzer &nbsp;·&nbsp; {result.get('info', {}).get('Category_Name', '')}
      &nbsp;·&nbsp; Comp confidence: {confidence}
    </div>
    """, unsafe_allow_html=True)