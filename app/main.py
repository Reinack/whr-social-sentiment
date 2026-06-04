"""
app/main.py
Página de inicio del dashboard WHR × Redes Sociales.
Ejecutar: streamlit run app/main.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import streamlit as st
import pandas as pd
from sqlalchemy import text
from db import get_session

st.set_page_config(
    page_title="WHR × Redes Sociales",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🌍 WHR × Redes Sociales")
st.subheader("Sentimiento digital vs. World Happiness Report 2026")

st.markdown("""
¿Lo que la gente expresa en redes sobre su bienestar **refleja o contradice**
lo que mide el WHR?

Este dashboard compara el sentimiento extraído de Reddit, YouTube y TSGI
con los 6 subindicadores del WHR para **19 países** en el período **2022–2024**.
""")

st.divider()

# ── Métricas de cobertura ──────────────────────────────────────────────────
try:
    with get_session() as session:
        counts = session.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM countries)                        AS paises,
                (SELECT COUNT(*) FROM posts WHERE sampled = TRUE)       AS posts,
                (SELECT COUNT(*) FROM classifications WHERE accepted = TRUE) AS clasificados,
                (SELECT COUNT(*) FROM tsgi_index)                       AS tsgi_dias,
                (SELECT COUNT(*) FROM analysis_results)                 AS resultados
        """)).fetchone()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Países",          counts.paises)
    c2.metric("Posts recolectados", f"{counts.posts:,}")
    c3.metric("Clasificados (≥0.7)", f"{counts.clasificados:,}")
    c4.metric("Días TSGI",       f"{counts.tsgi_dias:,}")
    c5.metric("Resultados WHR",  f"{counts.resultados:,}")

except Exception as e:
    st.warning(f"Base de datos no disponible: {e}")
    st.info("Iniciá el contenedor con `docker compose up -d` y recargá.")

st.divider()

# ── Cobertura por país y plataforma ───────────────────────────────────────
st.subheader("Cobertura actual")

try:
    with get_session() as session:
        rows = session.execute(text("""
            SELECT * FROM v_coverage ORDER BY iso2, platform
        """)).fetchall()

    if rows:
        df = pd.DataFrame([dict(r._mapping) for r in rows])
        st.dataframe(
            df,
            column_config={
                "iso2":               st.column_config.TextColumn("País"),
                "country_name":       st.column_config.TextColumn("Nombre"),
                "platform":           st.column_config.TextColumn("Plataforma"),
                "total_posts":        st.column_config.NumberColumn("Posts"),
                "classified":         st.column_config.NumberColumn("Clasif."),
                "accepted":           st.column_config.NumberColumn("Aceptados"),
                "acceptance_rate_pct":st.column_config.NumberColumn("Tasa %", format="%.1f"),
                "earliest":           st.column_config.TextColumn("Desde"),
                "latest":             st.column_config.TextColumn("Hasta"),
            },
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("Sin datos aún — ejecutá el pipeline primero.")

except Exception:
    st.info("Sin datos aún — ejecutá el pipeline primero.")

# ── Navegación ────────────────────────────────────────────────────────────
st.divider()
st.markdown("""
### Páginas del dashboard

| Página | Descripción |
|---|---|
| 🗺️ **Mapa de Brechas** | Mapa mundial: gap sentimiento vs. WHR por subindicador |
| 🔥 **Heatmap** | País × subindicador: sentimiento neto y gap |
| 📈 **Serie Temporal** | Evolución mensual del sentimiento 2022-2024 |
| ⚖️ **Comparador de Países** | Comparación directa entre dos países |

Navegá usando el menú de la izquierda.
""")
