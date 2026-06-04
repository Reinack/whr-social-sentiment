"""
Página 4 — Comparador directo entre dos países.
Radar chart + tabla de brechas por subindicador.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from sqlalchemy import text
from db import get_session
from app.components.filters import sidebar_filters, PAISES

st.set_page_config(page_title="Comparador", page_icon="⚖️", layout="wide")
st.title("⚖️ Comparador de Países")

f = sidebar_filters(show_subindicador=False, show_paises=False)

col1, col2 = st.columns(2)
pais_a = col1.selectbox("País A", PAISES, index=0)
pais_b = col2.selectbox("País B", PAISES, index=1)

if pais_a == pais_b:
    st.warning("Seleccioná dos países distintos.")
    st.stop()

LABEL_SUB = {
    "apoyo_social": "Apoyo social",
    "libertad":     "Libertad",
    "economia_pib": "Economía/PIB",
    "salud":        "Salud",
    "generosidad":  "Generosidad",
    "corrupcion":   "Corrupción",
}


@st.cache_data(ttl=300)
def load_country(iso2, plataforma, years):
    filters = ["c.iso2 = :iso2"]
    params  = {"iso2": iso2}
    if plataforma:
        filters.append("pl.slug = :plat")
        params["plat"] = plataforma
    if years:
        filters.append("ar.year = ANY(:years)")
        params["years"] = years

    where = "WHERE " + " AND ".join(filters)

    with get_session() as session:
        rows = session.execute(text(f"""
            SELECT
                c.name_es,
                c.whr_rank_2025,
                c.whr_score_2025,
                ar.subindicator,
                ROUND(AVG(ar.sentiment_net)::numeric, 4)       AS avg_sentiment,
                ROUND(AVG(ar.whr_score_normalized)::numeric, 4) AS avg_whr_norm,
                ROUND(AVG(ar.gap)::numeric, 4)                  AS avg_gap,
                SUM(ar.sample_size)                             AS total_posts
            FROM analysis_results ar
            JOIN countries c  ON c.id  = ar.country_id
            JOIN platforms pl ON pl.id = ar.platform_id
            {where}
            GROUP BY c.name_es, c.whr_rank_2025, c.whr_score_2025, ar.subindicator
        """), params).fetchall()

    return pd.DataFrame([dict(r._mapping) for r in rows])


try:
    plat  = f.get("plataforma")
    years = tuple(f.get("years", []))

    da = load_country(pais_a, plat, years)
    db_ = load_country(pais_b, plat, years)

    if da.empty or db_.empty:
        st.info("Sin datos para uno o ambos países — ejecutá el pipeline o ajustá los filtros.")
        st.stop()

    da["label"] = da["subindicator"].map(LABEL_SUB).fillna(da["subindicator"])
    db_["label"] = db_["subindicator"].map(LABEL_SUB).fillna(db_["subindicator"])

    nombre_a = da["name_es"].iloc[0] if not da.empty else pais_a
    nombre_b = db_["name_es"].iloc[0] if not db_.empty else pais_b

    # ── Header con info WHR ────────────────────────────────────
    ca, cb = st.columns(2)
    ca.metric(nombre_a,
              f"WHR #{da['whr_rank_2025'].iloc[0]}",
              f"Score {da['whr_score_2025'].iloc[0]}")
    cb.metric(nombre_b,
              f"WHR #{db_['whr_rank_2025'].iloc[0]}",
              f"Score {db_['whr_score_2025'].iloc[0]}")

    st.divider()

    # ── Radar chart — sentimiento neto ────────────────────────
    subs_a = da.set_index("label")["avg_sentiment"].reindex(list(LABEL_SUB.values())).fillna(0)
    subs_b = db_.set_index("label")["avg_sentiment"].reindex(list(LABEL_SUB.values())).fillna(0)
    labels = list(LABEL_SUB.values())

    fig = go.Figure()
    for vals, nombre, color in [
        (subs_a, nombre_a, "#1f77b4"),
        (subs_b, nombre_b, "#ff7f0e"),
    ]:
        fig.add_trace(go.Scatterpolar(
            r=list(vals) + [vals.iloc[0]],
            theta=labels + [labels[0]],
            fill="toself",
            name=nombre,
            line_color=color,
            opacity=0.7,
        ))

    fig.update_layout(
        polar=dict(radialaxis=dict(visible=True, range=[-1, 1])),
        title="Sentimiento neto por subindicador",
        height=480,
        legend=dict(orientation="h", y=-0.15),
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── Tabla comparativa ──────────────────────────────────────
    st.subheader("Detalle por subindicador")

    merged = da[["label","avg_sentiment","avg_gap","total_posts"]].merge(
        db_[["label","avg_sentiment","avg_gap","total_posts"]],
        on="label", suffixes=(f"_{pais_a}", f"_{pais_b}")
    )
    merged["diff_sentiment"] = (
        merged[f"avg_sentiment_{pais_a}"] - merged[f"avg_sentiment_{pais_b}"]
    ).round(4)

    st.dataframe(
        merged.rename(columns={"label": "Subindicador"}),
        column_config={
            "Subindicador":                    st.column_config.TextColumn(),
            f"avg_sentiment_{pais_a}":         st.column_config.NumberColumn(f"Sentim. {pais_a}", format="%.4f"),
            f"avg_sentiment_{pais_b}":         st.column_config.NumberColumn(f"Sentim. {pais_b}", format="%.4f"),
            f"avg_gap_{pais_a}":               st.column_config.NumberColumn(f"Gap {pais_a}", format="%.4f"),
            f"avg_gap_{pais_b}":               st.column_config.NumberColumn(f"Gap {pais_b}", format="%.4f"),
            "diff_sentiment":                  st.column_config.NumberColumn("Diferencia A−B", format="%.4f"),
            f"total_posts_{pais_a}":           st.column_config.NumberColumn(f"Posts {pais_a}"),
            f"total_posts_{pais_b}":           st.column_config.NumberColumn(f"Posts {pais_b}"),
        },
        hide_index=True,
        use_container_width=True,
    )

except Exception as e:
    st.error(f"Error: {e}")
