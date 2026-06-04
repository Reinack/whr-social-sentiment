"""
Página 2 — Heatmap país × subindicador.
Muestra sentimiento neto o gap, por plataforma y año.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import text
from db import get_session
from app.components.filters import sidebar_filters

st.set_page_config(page_title="Heatmap", page_icon="🔥", layout="wide")
st.title("🔥 Heatmap — País × Subindicador")

f = sidebar_filters(show_subindicador=False)

metrica = st.radio(
    "Métrica",
    ["Sentimiento neto", "Gap vs. WHR"],
    horizontal=True,
)
col_metrica = "avg_sentiment" if metrica == "Sentimiento neto" else "avg_gap"


@st.cache_data(ttl=300)
def load_heatmap(plataforma, years):
    filters, params = [], {}
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
                c.name_es,
                ar.subindicator,
                ROUND(AVG(ar.sentiment_net)::numeric, 4) AS avg_sentiment,
                ROUND(AVG(ar.gap)::numeric, 4)           AS avg_gap,
                SUM(ar.sample_size)                       AS total_posts
            FROM analysis_results ar
            JOIN countries c  ON c.id  = ar.country_id
            JOIN platforms pl ON pl.id = ar.platform_id
            {where}
            GROUP BY c.name_es, ar.subindicator
        """), params).fetchall()

    return pd.DataFrame([dict(r._mapping) for r in rows])


LABEL_SUB = {
    "apoyo_social": "Apoyo social",
    "libertad":     "Libertad",
    "economia_pib": "Economía/PIB",
    "salud":        "Salud",
    "generosidad":  "Generosidad",
    "corrupcion":   "Corrupción",
}

try:
    df = load_heatmap(f.get("plataforma"), tuple(f.get("years", [])))

    if df.empty:
        st.info("Sin resultados — ejecutá el pipeline o ajustá los filtros.")
    else:
        df["subindicador_label"] = df["subindicator"].map(LABEL_SUB).fillna(df["subindicator"])

        pivot = df.pivot_table(
            index="name_es",
            columns="subindicador_label",
            values=col_metrica,
            aggfunc="mean",
        ).round(3)

        # Ordenar países por gap/sentimiento promedio
        pivot = pivot.reindex(pivot.mean(axis=1).sort_values().index)

        midpoint = 0 if col_metrica == "avg_gap" else pivot.stack().mean()
        cscale   = "RdYlGn" if col_metrica == "avg_gap" else "Blues"

        fig = px.imshow(
            pivot,
            color_continuous_scale=cscale,
            color_continuous_midpoint=midpoint,
            aspect="auto",
            labels={"color": metrica, "x": "Subindicador", "y": "País"},
            title=f"{metrica} por País y Subindicador",
            text_auto=".3f",
        )
        fig.update_layout(
            height=600,
            margin=dict(l=0, r=0, t=40, b=0),
            xaxis_title="",
            yaxis_title="",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.caption(
            "**Gap positivo** = redes más optimistas que el WHR  |  "
            "**Gap negativo** = redes más pesimistas que el WHR"
            if col_metrica == "avg_gap"
            else "Sentimiento neto = (positivos − negativos) / total. Rango: −1 a 1."
        )

        with st.expander("Ver datos"):
            st.dataframe(df.sort_values(["name_es","subindicator"]), hide_index=True, use_container_width=True)

except Exception as e:
    st.error(f"Error: {e}")
