"""
app/components/filters.py
Sidebar compartido: filtros reutilizables en todas las páginas.
"""
import streamlit as st

SUBINDICADORES = {
    "Todos":         None,
    "Apoyo social":  "apoyo_social",
    "Libertad":      "libertad",
    "Economía/PIB":  "economia_pib",
    "Salud":         "salud",
    "Generosidad":   "generosidad",
    "Corrupción":    "corrupcion",
}

PLATAFORMAS = {
    "Todas":   None,
    "Reddit":  "reddit",
    "YouTube": "youtube",
    "TSGI":    "tsgi",
}

PAISES = [
    "US","GB","CA","AU","BR","IN","MX","AR","DE","FR",
    "PH","JP","ZA","IT","PL","TR","KR","ID","VN",
]


def sidebar_filters(
    show_subindicador: bool = True,
    show_plataforma:   bool = True,
    show_paises:       bool = False,
    show_year:         bool = True,
) -> dict:
    """
    Renderiza los filtros en el sidebar y retorna un dict con los valores.
    Usar en cada página: f = sidebar_filters(); f["subindicador"] ...
    """
    st.sidebar.header("Filtros")

    result = {}

    if show_subindicador:
        label = st.sidebar.selectbox("Subindicador WHR", list(SUBINDICADORES.keys()))
        result["subindicador"] = SUBINDICADORES[label]

    if show_plataforma:
        label = st.sidebar.selectbox("Plataforma", list(PLATAFORMAS.keys()))
        result["plataforma"] = PLATAFORMAS[label]

    if show_year:
        result["years"] = st.sidebar.multiselect(
            "Años", [2022, 2023, 2024], default=[2022, 2023, 2024]
        )

    if show_paises:
        result["paises"] = st.sidebar.multiselect(
            "Países", PAISES, default=PAISES
        )

    st.sidebar.divider()
    st.sidebar.caption("WHR × Redes Sociales · 2022-2024")

    return result
