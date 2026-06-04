"""
Página 3 — Serie temporal mensual de sentimiento por país.
Overlay opcional con el índice TSGI (benchmark X/Twitter).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from sqlalchemy import text
from db import get_session
from app.components.filters import sidebar_filters, PAISES

st.set_page_config(page_title="Serie Temporal", page_icon="📈", layout="wide")
st.title("📈 Serie Temporal — Sentimiento 2022-2024")

f = sidebar_filters(show_paises=True, show_year=False)

show_tsgi = st.sidebar.toggle("Superponer TSGI", value=True)

paises_sel = f.get("paises", PAISES[:5])
if not paises_sel:
    st.warning("Seleccioná al menos un país.")
    st.stop()


@st.cache_data(ttl=300)
def load_serie(paises, subindicador, plataforma):
    filters = ["c.iso2 = ANY(:paises)"]
    params  = {"paises": list(paises)}

    if subindicador:
        filters.append("ar.subindicator = :sub")
        params["sub"] = subindicador
    if plataforma:
        filters.append("pl.slug = :plat")
        params["plat"] = plataforma

    where = "WHERE " + " AND ".join(filters)

    with get_session() as session:
        rows = session.execute(text(f"""
            SELECT
                c.iso2,
                c.name_es,
                ar.year,
                ar.month,
                ROUND(AVG(ar.sentiment_net)::numeric, 4)  AS sentiment_net,
                SUM(ar.sample_size)                        AS posts
            FROM analysis_results ar
            JOIN countries c  ON c.id  = ar.country_id
            JOIN platforms pl ON pl.id = ar.platform_id
            {where}
            GROUP BY c.iso2, c.name_es, ar.year, ar.month
            ORDER BY c.iso2, ar.year, ar.month
        """), params).fetchall()

    df = pd.DataFrame([dict(r._mapping) for r in rows])
    if not df.empty:
        df["fecha"] = pd.to_datetime(df[["year","month"]].assign(day=1))
    return df


@st.cache_data(ttl=300)
def load_tsgi(paises):
    with get_session() as session:
        rows = session.execute(text("""
            SELECT
                c.iso2,
                DATE_TRUNC('month', t.index_date)::date AS fecha,
                ROUND(AVG(t.sentiment_score)::numeric, 4) AS tsgi_score
            FROM tsgi_index t
            JOIN countries c ON c.id = t.country_id
            WHERE c.iso2 = ANY(:paises)
            GROUP BY c.iso2, DATE_TRUNC('month', t.index_date)
            ORDER BY c.iso2, fecha
        """), {"paises": list(paises)}).fetchall()

    df = pd.DataFrame([dict(r._mapping) for r in rows])
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"])
    return df


try:
    df = load_serie(tuple(paises_sel), f.get("subindicador"), f.get("plataforma"))
    df_tsgi = load_tsgi(tuple(paises_sel)) if show_tsgi else pd.DataFrame()

    if df.empty and df_tsgi.empty:
        st.info("Sin datos — ejecutá el pipeline o ajustá los filtros.")
        st.stop()

    fig = go.Figure()
    colors = px_colors = [
        "#1f77b4","#ff7f0e","#2ca02c","#d62728","#9467bd",
        "#8c564b","#e377c2","#7f7f7f","#bcbd22","#17becf",
        "#aec7e8","#ffbb78","#98df8a","#ff9896","#c5b0d5",
        "#c49c94","#f7b6d2","#c7c7c7","#dbdb8d",
    ]

    for i, iso2 in enumerate(paises_sel):
        color = colors[i % len(colors)]
        sub   = df[df["iso2"] == iso2]
        if not sub.empty:
            fig.add_trace(go.Scatter(
                x=sub["fecha"], y=sub["sentiment_net"],
                name=iso2,
                line=dict(color=color, width=2),
                mode="lines+markers",
                marker=dict(size=5),
                hovertemplate=f"<b>{iso2}</b><br>%{{x|%Y-%m}}<br>Sentim.: %{{y:.3f}}<extra></extra>",
            ))

        if show_tsgi and not df_tsgi.empty:
            sub_t = df_tsgi[df_tsgi["iso2"] == iso2]
            if not sub_t.empty:
                fig.add_trace(go.Scatter(
                    x=sub_t["fecha"], y=sub_t["tsgi_score"],
                    name=f"{iso2} TSGI",
                    line=dict(color=color, width=1, dash="dot"),
                    opacity=0.6,
                    mode="lines",
                    hovertemplate=f"<b>{iso2} TSGI</b><br>%{{x|%Y-%m}}<br>Score: %{{y:.4f}}<extra></extra>",
                ))

    fig.update_layout(
        title="Sentimiento mensual por país (línea continua = redes, punteada = TSGI)",
        xaxis_title="Mes",
        yaxis_title="Sentimiento neto / Score TSGI",
        height=520,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.4)
    st.plotly_chart(fig, use_container_width=True)

    if show_tsgi:
        st.caption("TSGI (punteado): índice de sentimiento de X/Twitter — MIT/Harvard. Solo cubre 2022-2023.")

except Exception as e:
    st.error(f"Error: {e}")
