"""
loaders/load_whr.py
Carga el archivo Excel WHR 2026 (Figura 2.1) a la tabla whr_scores.
Filtra solo los 19 países del estudio y los años 2022-2024.

Uso:
    python -m loaders.load_whr --path data/WHR26_Data_Figure_2_1.xlsx
    python -m loaders.load_whr --path data/WHR26_Data_Figure_2_1.xlsx --dry-run
"""
import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from sqlalchemy import text
from db import get_session

# Años de la ventana temporal del proyecto
YEARS = [2022, 2023, 2024]

# Mapeo nombre WHR → iso2
# Ajustar si el Excel usa nombres distintos
COUNTRY_NAME_TO_ISO2 = {
    "United States":              "US",
    "United Kingdom":             "GB",
    "Canada":                     "CA",
    "Australia":                  "AU",
    "Brazil":                     "BR",
    "India":                      "IN",
    "Mexico":                     "MX",
    "Argentina":                  "AR",
    "Germany":                    "DE",
    "France":                     "FR",
    "Philippines":                "PH",
    "Japan":                      "JP",
    "South Africa":               "ZA",
    "Italy":                      "IT",
    "Poland":                     "PL",
    "Türkiye":                    "TR",  # nombre oficial en WHR
    "Turkey":                     "TR",  # alias por si acaso
    "Republic of Korea":          "KR",
    "Indonesia":                  "ID",
    "Viet Nam":                   "VN",
    "Vietnam":                    "VN",  # alias
}

# Mapeo columnas Excel → campos de la tabla
COL_MAP = {
    "Life evaluation (3-year average)":         "score",
    "Explained by: Log GDP per capita":          "gdp",
    "Explained by: Social support":              "social_support",
    "Explained by: Healthy life expectancy":     "healthy_life",
    "Explained by: Freedom to make life choices":"freedom",
    "Explained by: Generosity":                  "generosity",
    "Explained by: Perceptions of corruption":   "corruption",
    "Dystopia + residual":                        "dystopia_residual",
}


def load_whr(path: str, dry_run: bool = False) -> dict:
    """
    Lee el Excel y carga los scores WHR en la base.
    Retorna un dict con estadísticas del proceso.
    """
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Cargando WHR desde: {path}")

    # ── Leer Excel ────────────────────────────────────────────
    df = pd.read_excel(path)

    # Normalizar nombres de columnas
    df.columns = df.columns.str.strip()

    # Filtrar ventana temporal
    df = df[df["Year"].isin(YEARS)].copy()
    print(f"  Filas en ventana 2022-2024: {len(df)}")

    # Mapear país → iso2
    df["iso2"] = df["Country name"].map(COUNTRY_NAME_TO_ISO2)
    matched   = df["iso2"].notna().sum()
    unmatched = df[df["iso2"].isna()]["Country name"].unique().tolist()
    print(f"  Países matcheados: {matched} / {len(df)}")
    if unmatched:
        print(f"  [!] Sin match (se ignoran): {unmatched[:10]}")

    # Solo países del estudio
    df = df[df["iso2"].notna()].copy()

    # Renombrar columnas de subindicadores
    df = df.rename(columns=COL_MAP)

    # Convertir Year a int limpio
    df["year"] = df["Year"].astype(int)
    df["rank"] = df["Rank"].astype("Int64")  # nullable int

    stats = {"rows_read": len(df), "inserted": 0, "updated": 0, "errors": 0}

    if dry_run:
        print("\n[DRY RUN] Preview de datos a insertar:")
        print(df[["iso2", "year", "score", "gdp", "social_support", "rank"]].to_string(index=False))
        return stats

    # ── Insertar en base ──────────────────────────────────────
    with get_session() as session:
        # Obtener mapping iso2 → country_id
        rows = session.execute(
            text("SELECT id, iso2 FROM countries")
        ).fetchall()
        iso2_to_id = {r.iso2: r.id for r in rows}

        for _, row in df.iterrows():
            iso2 = row["iso2"]
            if iso2 not in iso2_to_id:
                print(f"  [!] País {iso2} no encontrado en tabla countries — omitido")
                stats["errors"] += 1
                continue

            country_id = iso2_to_id[iso2]

            # Construir dict con valores, convirtiendo NaN → None
            values = {
                "country_id":       country_id,
                "year":             int(row["year"]),
                "score":            _safe_float(row.get("score")),
                "gdp":              _safe_float(row.get("gdp")),
                "social_support":   _safe_float(row.get("social_support")),
                "healthy_life":     _safe_float(row.get("healthy_life")),
                "freedom":          _safe_float(row.get("freedom")),
                "generosity":       _safe_float(row.get("generosity")),
                "corruption":       _safe_float(row.get("corruption")),
                "dystopia_residual":_safe_float(row.get("dystopia_residual")),
                "rank":             int(row["rank"]) if pd.notna(row.get("rank")) else None,
                "source_file":      os.path.basename(path),
            }

            # UPSERT — actualiza si ya existe (country_id, year)
            result = session.execute(text("""
                INSERT INTO whr_scores
                    (country_id, year, score, gdp, social_support, healthy_life,
                     freedom, generosity, corruption, dystopia_residual, rank, source_file)
                VALUES
                    (:country_id, :year, :score, :gdp, :social_support, :healthy_life,
                     :freedom, :generosity, :corruption, :dystopia_residual, :rank, :source_file)
                ON CONFLICT (country_id, year)
                DO UPDATE SET
                    score             = EXCLUDED.score,
                    gdp               = EXCLUDED.gdp,
                    social_support    = EXCLUDED.social_support,
                    healthy_life      = EXCLUDED.healthy_life,
                    freedom           = EXCLUDED.freedom,
                    generosity        = EXCLUDED.generosity,
                    corruption        = EXCLUDED.corruption,
                    dystopia_residual = EXCLUDED.dystopia_residual,
                    rank              = EXCLUDED.rank,
                    source_file       = EXCLUDED.source_file,
                    loaded_at         = NOW()
                RETURNING xmax  -- 0 = INSERT, >0 = UPDATE
            """), values)

            xmax = result.scalar()
            if xmax == 0:
                stats["inserted"] += 1
            else:
                stats["updated"] += 1

    print(f"\n[OK] Completado:")
    print(f"  Insertados: {stats['inserted']}")
    print(f"  Actualizados: {stats['updated']}")
    print(f"  Errores: {stats['errors']}")
    return stats


def _safe_float(val) -> float | None:
    """Convierte NaN/None a None, cualquier otro valor a float."""
    if val is None:
        return None
    try:
        import math
        return None if math.isnan(float(val)) else round(float(val), 4)
    except (TypeError, ValueError):
        return None


def verify_load():
    """Verifica la carga mostrando un resumen por país y año."""
    with get_session() as session:
        rows = session.execute(text("""
            SELECT c.iso2, c.name_es, w.year, w.score, w.rank
            FROM whr_scores w
            JOIN countries c ON c.id = w.country_id
            ORDER BY w.year, w.rank
        """)).fetchall()

    if not rows:
        print("[!] No hay datos en whr_scores")
        return

    print(f"\n{'ISO2':6} {'País':20} {'Año':6} {'Score':8} {'Rank':6}")
    print("-" * 50)
    for r in rows:
        print(f"{r.iso2:6} {r.name_es:20} {r.year:6} {r.score or 'N/A':8} {r.rank or '-':6}")
    print(f"\nTotal: {len(rows)} registros")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Carga datos WHR a PostgreSQL")
    parser.add_argument("--path", default=os.getenv("WHR_EXCEL_PATH", "data/WHR26_Data_Figure_2_1.xlsx"))
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra los datos, no inserta")
    parser.add_argument("--verify", action="store_true", help="Muestra resumen de datos cargados")
    args = parser.parse_args()

    if args.verify:
        verify_load()
    else:
        load_whr(args.path, dry_run=args.dry_run)

