"""
loaders/load_tsgi.py
Carga el dataset TSGI (Twitter Standardized Global Index) de MIT/Harvard
a la tabla tsgi_index.

Fuente: Harvard Dataverse — doi.org/10.7910/DVN/3IL00Q
Licencia: CC BY 4.0
Formato: CSV con columnas country_code, date, sentiment, tweet_count

El TSGI es un índice de sentimiento agregado por país/día construido a partir
de ~4.300 millones de tweets geoetiquetados. No contiene texto crudo.
Se usa como benchmark de validación cruzada para el sentimiento general,
no como fuente de clasificación por subindicador.

Uso:
    python -m loaders.load_tsgi --path data/tsgi/TSGI_2022_2024.csv
    python -m loaders.load_tsgi --path data/tsgi/ --dry-run
    python -m loaders.load_tsgi --verify
"""
import argparse
import os
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from sqlalchemy import text
from db import get_session

# Ventana temporal del proyecto
START_DATE = date(2022, 1, 1)
END_DATE   = date(2024, 12, 31)

# ISO2 de los 19 países del estudio
STUDY_COUNTRIES = {
    "US", "GB", "CA", "AU", "BR", "IN", "MX", "AR", "DE", "FR",
    "PH", "JP", "ZA", "IT", "PL", "TR", "KR", "ID", "VN",
}

# El TSGI puede usar ISO2, ISO3, o nombres. Mapeamos variantes conocidas.
ISO3_TO_ISO2 = {
    "USA": "US", "GBR": "GB", "CAN": "CA", "AUS": "AU", "BRA": "BR",
    "IND": "IN", "MEX": "MX", "ARG": "AR", "DEU": "DE", "FRA": "FR",
    "PHL": "PH", "JPN": "JP", "ZAF": "ZA", "ITA": "IT", "POL": "PL",
    "TUR": "TR", "KOR": "KR", "IDN": "ID", "VNM": "VN",
}

# Nombres alternativos que pueden aparecer en el dataset
NAME_TO_ISO2 = {
    "United States":  "US", "United Kingdom": "GB", "Canada":       "CA",
    "Australia":      "AU", "Brazil":          "BR", "India":        "IN",
    "Mexico":         "MX", "Argentina":       "AR", "Germany":      "DE",
    "France":         "FR", "Philippines":     "PH", "Japan":        "JP",
    "South Africa":   "ZA", "Italy":           "IT", "Poland":       "PL",
    "Turkey":         "TR", "Türkiye":         "TR", "South Korea":  "KR",
    "Korea":          "KR", "Indonesia":       "ID", "Vietnam":      "VN",
    "Viet Nam":       "VN",
}

# Columnas esperadas en el CSV del TSGI — intentamos múltiples variantes
# porque el formato puede diferir entre versiones del dataset
COUNTRY_COL_CANDIDATES = ["NAME_0", "country_code", "country", "iso2", "iso3", "Country", "ISO2", "ISO3"]
DATE_COL_CANDIDATES    = ["DATE", "date", "Date", "index_date", "day", "time"]
SCORE_COL_CANDIDATES   = ["SCORE", "sentiment", "sentiment_score", "index", "value", "score", "Sentiment"]
COUNT_COL_CANDIDATES   = ["N", "tweet_count", "tweets", "n_tweets", "count", "Count", "TweetCount"]


def load_tsgi(path: str, dry_run: bool = False) -> dict:
    """
    Lee el CSV del TSGI y carga los índices en tsgi_index.
    'path' puede ser un archivo .csv o un directorio que contenga archivos .csv.
    """
    csv_files = _resolve_files(path)
    if not csv_files:
        print(f"✗ No se encontraron archivos CSV en: {path}")
        return {"errors": 1}

    all_frames = []
    for f in csv_files:
        print(f"  Leyendo: {f.name}")
        try:
            df = _read_tsgi_file(f)
            if df is not None and not df.empty:
                all_frames.append(df)
        except Exception as e:
            print(f"  ✗ Error leyendo {f.name}: {e}")

    if not all_frames:
        print("✗ Ningún archivo se pudo procesar")
        return {"errors": 1}

    data = pd.concat(all_frames, ignore_index=True)
    print(f"\n  Filas combinadas: {len(data)}")

    # Filtrar ventana temporal
    data = data[
        (data["index_date"] >= START_DATE) &
        (data["index_date"] <= END_DATE)
    ].copy()
    print(f"  Filas en ventana 2022-2024: {len(data)}")

    # Filtrar países del estudio
    data = data[data["iso2"].isin(STUDY_COUNTRIES)].copy()
    print(f"  Filas para los 19 países: {len(data)}")

    if data.empty:
        print("  ⚠ Sin datos tras filtros — verificar columnas del CSV")
        return {"no_data": 1}

    if dry_run:
        print("\n[DRY RUN] Preview:")
        print(data[["iso2", "index_date", "sentiment_score", "tweet_count"]].head(10).to_string(index=False))
        return {"rows": len(data)}

    stats = _insert_to_db(data)
    print(f"\n✓ Completado: {stats['inserted']} insertados | {stats['updated']} actualizados | {stats['errors']} errores")
    return stats


def _resolve_files(path: str) -> list[Path]:
    """Retorna lista de archivos CSV o TAB a procesar."""
    p = Path(path)
    if p.is_file() and p.suffix.lower() in (".csv", ".tab"):
        return [p]
    if p.is_dir():
        files = sorted(p.glob("*.csv")) + sorted(p.glob("*.tab"))
        return sorted(files)
    return []


def _read_tsgi_file(filepath: Path) -> pd.DataFrame | None:
    """
    Lee un archivo CSV del TSGI y lo normaliza al formato interno:
    columns: iso2, index_date (date), sentiment_score (float), tweet_count (int)
    """
    sep = "\t" if filepath.suffix.lower() == ".tab" else ","
    df = pd.read_csv(filepath, sep=sep, low_memory=False)
    df.columns = df.columns.str.strip()

    # ── Detectar columna de país ───────────────────────────────
    country_col = _find_col(df.columns, COUNTRY_COL_CANDIDATES)
    if not country_col:
        print(f"  ✗ No se encontró columna de país. Columnas disponibles: {list(df.columns)}")
        return None

    # ── Detectar columna de fecha ──────────────────────────────
    date_col = _find_col(df.columns, DATE_COL_CANDIDATES)
    if not date_col:
        print(f"  ✗ No se encontró columna de fecha. Columnas disponibles: {list(df.columns)}")
        return None

    # ── Detectar columna de sentimiento ───────────────────────
    score_col = _find_col(df.columns, SCORE_COL_CANDIDATES)
    if not score_col:
        print(f"  ✗ No se encontró columna de sentimiento. Columnas disponibles: {list(df.columns)}")
        return None

    # ── Detectar columna de conteo (opcional) ─────────────────
    count_col = _find_col(df.columns, COUNT_COL_CANDIDATES)

    # ── Normalizar país → iso2 ────────────────────────────────
    df["iso2"] = df[country_col].apply(_normalize_country)

    # ── Normalizar fecha ──────────────────────────────────────
    df["index_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date

    # ── Normalizar score ──────────────────────────────────────
    df["sentiment_score"] = pd.to_numeric(df[score_col], errors="coerce")

    # ── Conteo de tweets (puede ser None si no está en el CSV) ─
    if count_col:
        df["tweet_count"] = pd.to_numeric(df[count_col], errors="coerce").astype("Int64")
    else:
        df["tweet_count"] = pd.NA

    # Descartar filas con valores críticos nulos
    df = df.dropna(subset=["iso2", "index_date", "sentiment_score"])

    return df[["iso2", "index_date", "sentiment_score", "tweet_count"]]


def _find_col(columns, candidates: list[str]) -> str | None:
    """Retorna el primer candidato que existe en las columnas del DataFrame."""
    col_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in columns:
            return cand
        if cand.lower() in col_lower:
            return col_lower[cand.lower()]
    return None


def _normalize_country(val: str) -> str | None:
    """Convierte ISO2, ISO3 o nombre completo a ISO2 del estudio."""
    if not isinstance(val, str):
        return None
    val = val.strip().upper()

    # Ya es ISO2
    if len(val) == 2 and val in STUDY_COUNTRIES:
        return val

    # Es ISO3
    if len(val) == 3 and val in ISO3_TO_ISO2:
        return ISO3_TO_ISO2[val]

    # Nombre completo (comparar en minúsculas)
    val_title = val.title()
    for name, iso2 in NAME_TO_ISO2.items():
        if name.upper() == val.upper() or name.title() == val_title:
            return iso2

    return None


def _insert_to_db(data: pd.DataFrame) -> dict:
    """Inserta o actualiza filas en tsgi_index."""
    stats = {"inserted": 0, "updated": 0, "errors": 0}

    with get_session() as session:
        # Obtener mapping iso2 → country_id
        rows = session.execute(text("SELECT id, iso2 FROM countries")).fetchall()
        iso2_to_id = {r.iso2: r.id for r in rows}

        for _, row in data.iterrows():
            iso2 = row["iso2"]
            if iso2 not in iso2_to_id:
                stats["errors"] += 1
                continue

            country_id = iso2_to_id[iso2]
            tweet_count = None if pd.isna(row["tweet_count"]) else int(row["tweet_count"])

            try:
                result = session.execute(text("""
                    INSERT INTO tsgi_index
                        (country_id, index_date, sentiment_score, tweet_count)
                    VALUES
                        (:country_id, :index_date, :sentiment_score, :tweet_count)
                    ON CONFLICT (country_id, index_date)
                    DO UPDATE SET
                        sentiment_score = EXCLUDED.sentiment_score,
                        tweet_count     = EXCLUDED.tweet_count,
                        loaded_at       = NOW()
                    RETURNING xmax
                """), {
                    "country_id":     country_id,
                    "index_date":     row["index_date"],
                    "sentiment_score": round(float(row["sentiment_score"]), 4),
                    "tweet_count":    tweet_count,
                })
                xmax = result.scalar()
                if xmax == 0:
                    stats["inserted"] += 1
                else:
                    stats["updated"] += 1

            except Exception as e:
                stats["errors"] += 1
                print(f"    ✗ Error en {iso2} / {row['index_date']}: {e}")

    return stats


def verify_load():
    """Muestra resumen de datos cargados en tsgi_index."""
    with get_session() as session:
        rows = session.execute(text("""
            SELECT
                c.iso2,
                c.name_es,
                COUNT(*)                            AS n_days,
                ROUND(MIN(t.sentiment_score)::numeric, 4)  AS min_score,
                ROUND(MAX(t.sentiment_score)::numeric, 4)  AS max_score,
                ROUND(AVG(t.sentiment_score)::numeric, 4)  AS avg_score,
                MIN(t.index_date)                   AS earliest,
                MAX(t.index_date)                   AS latest
            FROM tsgi_index t
            JOIN countries c ON c.id = t.country_id
            GROUP BY c.iso2, c.name_es
            ORDER BY c.iso2
        """)).fetchall()

    if not rows:
        print("⚠ No hay datos en tsgi_index")
        return

    print(f"\n{'ISO2':6} {'País':20} {'Días':7} {'Min':8} {'Max':8} {'Avg':8} {'Desde':12} {'Hasta':12}")
    print("─" * 85)
    for r in rows:
        print(
            f"{r.iso2:6} {r.name_es:20} {r.n_days:7} "
            f"{r.min_score or 'N/A':8} {r.max_score or 'N/A':8} "
            f"{r.avg_score or 'N/A':8} "
            f"{str(r.earliest):12} {str(r.latest):12}"
        )
    print(f"\nTotal: {sum(r.n_days for r in rows)} registros diarios")


def show_monthly_summary():
    """Muestra resumen mensual de TSGI para los países del estudio."""
    with get_session() as session:
        rows = session.execute(text("""
            SELECT
                c.iso2,
                DATE_TRUNC('month', t.index_date)   AS month,
                COUNT(*)                             AS n_days,
                ROUND(AVG(t.sentiment_score)::numeric, 4) AS avg_score
            FROM tsgi_index t
            JOIN countries c ON c.id = t.country_id
            GROUP BY c.iso2, DATE_TRUNC('month', t.index_date)
            ORDER BY c.iso2, month
        """)).fetchall()

    if not rows:
        print("⚠ Sin datos")
        return

    current_country = None
    for r in rows:
        if r.iso2 != current_country:
            current_country = r.iso2
            print(f"\n{r.iso2}")
        month_str = str(r.month)[:7]
        bar = "█" * int((r.avg_score or 0) * 20)
        print(f"  {month_str}  {r.avg_score:.4f}  {bar}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Carga el TSGI MIT/Harvard a la tabla tsgi_index"
    )
    parser.add_argument(
        "--path",
        default=os.getenv("TSGI_PATH", "data/tsgi/"),
        help="Archivo .csv o directorio con archivos .csv del TSGI"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo muestra los datos, no inserta en la base")
    parser.add_argument("--verify", action="store_true",
                        help="Muestra resumen de datos ya cargados")
    parser.add_argument("--monthly", action="store_true",
                        help="Muestra resumen mensual con sparklines")
    args = parser.parse_args()

    if args.verify:
        verify_load()
    elif args.monthly:
        show_monthly_summary()
    else:
        if not Path(args.path).exists():
            print(f"✗ Ruta no encontrada: {args.path}")
            print("  Descargar el dataset desde: https://doi.org/10.7910/DVN/3IL00Q")
            print("  Colocar el CSV en data/tsgi/ o ajustar TSGI_PATH en .env")
            sys.exit(1)

        stats = load_tsgi(args.path, dry_run=args.dry_run)
