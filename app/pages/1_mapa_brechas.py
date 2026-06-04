"""
Página 1 — Mapa mundial de brechas sentimiento vs. WHR.
Gap positivo = más feliz en redes que en el WHR oficial.
Gap negativo = más infeliz en redes que en el WHR oficial.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import text
from db import get_session
from app.components.filters import sidebar_filters

st.set_page_config(page_title="Mapa de Brechas", page_icon="🗺️", layout="wide")
st.title("🗺️ Mapa de Brechas — Sentimiento vs. WHR")
st.caption("Gap = sentimiento normalizado − score WHR normalizado. Positivo → redes más optimistas que el WHR.")

f = sidebar_filters(show_paises=False)

# ── Consulta ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_gap_data(subindicador, plataforma, years):
    filters = []
    params  = {}

    if subindicador:
        filters.append("ar.subindicator = :sub")
        params["sub"] = subindicador
    if plataforma:
        filters.append("pl.slug = :plat")
        params["plat"] = plataforma
    if years:
        filters.append("ar.year = ANY(:years)")
        params["years"] = years

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    with get_session() as session:
        rows = session.execute(text(f"""
            SELECT
                c.iso2,
                c.iso3,
                c.name_es,
                c.whr_rank_2025,
                ar.subindicator,
                ROUND(AVG(ar.gap)::numeric, 4)             AS avg_gap,
                ROUND(AVG(ar.sentiment_net)::numeric, 4)   AS avg_sentiment,
                ROUND(AVG(ar.whr_score_normalized)::numeric, 4) AS avg_whr_norm,
                SUM(ar.sample_size)                         AS total_posts
            FROM analysis_results ar
            JOIN countries c  ON c.id  = ar.country_id
            JOIN platforms pl ON pl.id = ar.platform_id
            {where}
            GROUP BY c.iso2, c.iso3, c.name_es, c.whr_rank_2025, ar.subindicator
        """), params).fetchall()

    return pd.DataFrame([dict(r._mapping) for r in rows])


try:
    df = load_gap_data(f.get("subindicador"), f.get("plataforma"), tuple(f.get("years", [])))

    if df.empty:
        st.info("Sin resultados — ejecutá el pipeline o ajustá los filtros.")
    else:
        # Agregar si hay múltiples subindicadores
        df_map = df.groupby(["iso2","iso3","name_es","whr_rank_2025"], as_index=False).agg(
            avg_gap=("avg_gap","mean"),
            avg_sentiment=("avg_sentiment","mean"),
            avg_whr_norm=("avg_whr_norm","mean"),
            total_posts=("total_posts","sum"),
        ).round(4)

        fig = px.choropleth(
            df_map,
            locations="iso3",
            color="avg_gap",
            hover_name="name_es",
            hover_data={
                "iso3": False,
                "avg_gap": ":.3f",
                "avg_sentiment": ":.3f",
                "avg_whr_norm": ":.3f",
                "whr_rank_2025": True,
                "total_posts": True,
            },
            color_continuous_scale="RdYlGn",
            color_continuous_midpoint=0,
            range_color=[-0.3, 0.3],
            labels={
                "avg_gap":       "Gap promedio",
                "avg_sentiment": "Sentim. neto",
                "avg_whr_norm":  "WHR norm.",
                "whr_rank_2025": "Ranking WHR 2025",
                "total_posts":   "Posts",
            },
            title="Brecha sentimiento digital vs. score WHR (2022-2024)",
        )
        fig.update_layout(
            margin=dict(l=0, r=0, t=40, b=0),
            coloraxis_colorbar=dict(title="Gap"),
            height=520,
        )
        st.plotly_chart(fig, use_container_width=True)

        # Tabla detalle
        st.subheader("Detalle por país")
        df_show = df_map.sort_values("avg_gap").copy()
        df_show["dirección"] = df_show["avg_gap"].apply(
            lambda g: "↑ Más optimista en redes" if g > 0.05
                      else ("↓ Más pesimista en redes" if g < -0.05 else "≈ Alineado")
        )
        st.dataframe(
            df_show[["iso2","name_es","whr_rank_2025","avg_gap","avg_sentiment","avg_whr_norm","total_posts","dirección"]],
            column_config={
                "iso2":          st.column_config.TextColumn("ISO2"),
                "name_es":       st.column_config.TextColumn("País"),
                "whr_rank_2025": st.column_config.NumberColumn("WHR #"),
                "avg_gap":       st.column_config.NumberColumn("Gap", format="%.4f"),
                "avg_sentiment": st.column_config.NumberColumn("Sentim.", format="%.4f"),
                "avg_whr_norm":  st.column_config.NumberColumn("WHR norm.", format="%.4f"),
                "total_posts":   st.column_config.NumberColumn("Posts"),
                "dirección":     st.column_config.TextColumn("Dirección"),
            },
            hide_index=True,
            use_container_width=True,
        )

except Exception as e:
    st.error(f"Error al cargar datos: {e}")
    st.info("Verificá que la base esté corriendo (`docker compose up -d`) y el pipeline ejecutado.")
