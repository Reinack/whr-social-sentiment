"""
analysis/compute_results.py
Agrega clasificaciones aceptadas y calcula el gap sentimiento vs WHR.
Puebla la tabla analysis_results con métricas mensuales por país × plataforma × subindicador.

Uso:
    python -m analysis.compute_results
    python -m analysis.compute_results --country AR
    python -m analysis.compute_results --show-gaps
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from sqlalchemy import text
from db import get_session

# Ventana temporal del proyecto
YEARS = [2022, 2023, 2024]

# Subindicadores WHR y su columna correspondiente en whr_scores
SUBINDICATOR_COL = {
    "apoyo_social":  "social_support",
    "libertad":      "freedom",
    "economia_pib":  "gdp",
    "salud":         "healthy_life",
    "generosidad":   "generosity",
    "corrupcion":    "corruption",
}


def compute_results(country_iso2: str | None = None) -> dict:
    """
    Calcula sentiment_net y gap WHR para cada combinación
    country × platform × subindicator × year × month.
    """
    print(f"\nComputando resultados{'  → ' + country_iso2 if country_iso2 else ' (todos los países)'}...")

    with get_session() as session:
        # ── 1. Obtener rangos WHR para normalización (todos los países del estudio)
        whr_ranges = _get_whr_ranges(session)

        # ── 2. Obtener rangos de sentimiento neto (para normalizar 0-1)
        # Se calcula después de agregar, por ahora usamos [-1, 1] → [0, 1]

        # ── 3. Agregar clasificaciones por country × platform × sub × year × month
        country_filter = "AND c.iso2 = :iso2" if country_iso2 else ""

        agg_rows = session.execute(text(f"""
            SELECT
                p.country_id,
                p.platform_id,
                cl.subindicator,
                EXTRACT(YEAR  FROM p.posted_at)::smallint AS year,
                EXTRACT(MONTH FROM p.posted_at)::smallint AS month,
                COUNT(*)                                           AS sample_size,
                COUNT(*) FILTER (WHERE cl.sentiment = 'positivo') AS n_positive,
                COUNT(*) FILTER (WHERE cl.sentiment = 'negativo') AS n_negative,
                COUNT(*) FILTER (WHERE cl.sentiment = 'neutro')   AS n_neutral,
                ROUND(AVG(cl.intensity)::numeric, 3)              AS avg_intensity,
                ROUND(AVG(cl.confidence)::numeric, 3)             AS avg_confidence
            FROM classifications cl
            JOIN posts      p  ON p.id  = cl.post_id
            JOIN countries  c  ON c.id  = p.country_id
            WHERE cl.accepted = TRUE
              AND cl.subindicator != 'ninguno'
              AND p.posted_at >= '2022-01-01'
              AND p.posted_at <  '2025-01-01'
              {country_filter}
            GROUP BY p.country_id, p.platform_id, cl.subindicator,
                     EXTRACT(YEAR FROM p.posted_at),
                     EXTRACT(MONTH FROM p.posted_at)
            ORDER BY p.country_id, cl.subindicator, year, month
        """), {"iso2": country_iso2}).fetchall()

        print(f"  Combinaciones a procesar: {len(agg_rows)}")
        if not agg_rows:
            print("  ⚠ Sin datos — ¿hay clasificaciones aceptadas en la base?")
            return {"rows": 0}

        # ── 4. Para cada fila, calcular métricas y upsert en analysis_results
        inserted = 0
        for row in agg_rows:
            # Sentimiento neto: (pos - neg) / total, rango [-1, 1]
            total = row.sample_size or 1
            sentiment_net = (row.n_positive - row.n_negative) / total

            # Normalizar sentimiento a [0, 1]
            sentiment_norm = (sentiment_net + 1) / 2

            # Obtener score WHR del subindicador para ese país y año
            whr_score = _get_whr_score(
                session,
                row.country_id,
                row.subindicator,
                int(row.year),
                whr_ranges,
            )
            whr_norm  = whr_score["normalized"] if whr_score else None
            whr_raw   = whr_score["raw"]        if whr_score else None

            # Gap = sentimiento_norm - whr_norm
            # Positivo → más feliz en redes que en WHR
            # Negativo → más infeliz en redes que en WHR
            gap = round(sentiment_norm - whr_norm, 4) if whr_norm is not None else None

            session.execute(text("""
                INSERT INTO analysis_results
                    (country_id, platform_id, subindicator, year, month,
                     n_positive, n_negative, n_neutral, sample_size,
                     sentiment_net, avg_intensity, avg_confidence,
                     whr_score, whr_score_normalized, sentiment_normalized, gap)
                VALUES
                    (:country_id, :platform_id, :subindicator, :year, :month,
                     :n_positive, :n_negative, :n_neutral, :sample_size,
                     :sentiment_net, :avg_intensity, :avg_confidence,
                     :whr_score, :whr_score_normalized, :sentiment_normalized, :gap)
                ON CONFLICT (country_id, platform_id, subindicator, year, month)
                DO UPDATE SET
                    n_positive           = EXCLUDED.n_positive,
                    n_negative           = EXCLUDED.n_negative,
                    n_neutral            = EXCLUDED.n_neutral,
                    sample_size          = EXCLUDED.sample_size,
                    sentiment_net        = EXCLUDED.sentiment_net,
                    avg_intensity        = EXCLUDED.avg_intensity,
                    avg_confidence       = EXCLUDED.avg_confidence,
                    whr_score            = EXCLUDED.whr_score,
                    whr_score_normalized = EXCLUDED.whr_score_normalized,
                    sentiment_normalized = EXCLUDED.sentiment_normalized,
                    gap                  = EXCLUDED.gap,
                    computed_at          = NOW()
            """), {
                "country_id":           row.country_id,
                "platform_id":          row.platform_id,
                "subindicator":         row.subindicator,
                "year":                 int(row.year),
                "month":                int(row.month),
                "n_positive":           row.n_positive,
                "n_negative":           row.n_negative,
                "n_neutral":            row.n_neutral,
                "sample_size":          row.sample_size,
                "sentiment_net":        round(sentiment_net, 4),
                "avg_intensity":        float(row.avg_intensity) if row.avg_intensity else None,
                "avg_confidence":       float(row.avg_confidence) if row.avg_confidence else None,
                "whr_score":            whr_raw,
                "whr_score_normalized": round(whr_norm, 4) if whr_norm else None,
                "sentiment_normalized": round(sentiment_norm, 4),
                "gap":                  gap,
            })
            inserted += 1

    print(f"  ✓ Filas upserted: {inserted}")
    return {"rows": inserted}


def _get_whr_ranges(session) -> dict:
    """
    Calcula min/max de cada subindicador WHR entre todos los países del estudio
    para normalizar a [0, 1].
    """
    ranges = {}
    for sub, col in SUBINDICATOR_COL.items():
        row = session.execute(text(f"""
            SELECT MIN({col}), MAX({col})
            FROM whr_scores
            WHERE {col} IS NOT NULL
              AND year IN (2022, 2023, 2024)
        """)).fetchone()
        if row and row[0] is not None:
            ranges[sub] = {"min": float(row[0]), "max": float(row[1])}
    return ranges


def _get_whr_score(
    session,
    country_id: int,
    subindicator: str,
    year: int,
    ranges: dict,
) -> dict | None:
    """Retorna score WHR raw y normalizado para un país, subindicador y año."""
    col = SUBINDICATOR_COL.get(subindicator)
    if not col:
        return None

    # Intentar año exacto; si no existe, tomar el más cercano
    row = session.execute(text(f"""
        SELECT {col} AS val
        FROM whr_scores
        WHERE country_id = :cid
          AND year = :year
          AND {col} IS NOT NULL
        LIMIT 1
    """), {"cid": country_id, "year": year}).fetchone()

    if not row:
        # Buscar año más cercano dentro de la ventana
        row = session.execute(text(f"""
            SELECT {col} AS val
            FROM whr_scores
            WHERE country_id = :cid
              AND year IN (2022, 2023, 2024)
              AND {col} IS NOT NULL
            ORDER BY ABS(year - :year)
            LIMIT 1
        """), {"cid": country_id, "year": year}).fetchone()

    if not row or row.val is None:
        return None

    raw = float(row.val)
    r = ranges.get(subindicator)
    if not r or r["max"] == r["min"]:
        return {"raw": raw, "normalized": 0.5}

    normalized = (raw - r["min"]) / (r["max"] - r["min"])
    return {"raw": raw, "normalized": round(normalized, 4)}


def show_gaps(top_n: int = 20):
    """Muestra los países con mayor brecha sentimiento vs WHR."""
    with get_session() as session:
        rows = session.execute(text("""
            SELECT
                c.iso2,
                c.name_es,
                c.whr_rank_2025,
                ar.subindicator,
                pl.slug                                         AS platform,
                ROUND(AVG(ar.sentiment_net)::numeric, 3)        AS avg_sentiment,
                ROUND(AVG(ar.whr_score_normalized)::numeric, 3) AS avg_whr_norm,
                ROUND(AVG(ar.gap)::numeric, 3)                  AS avg_gap,
                SUM(ar.sample_size)                             AS posts
            FROM analysis_results ar
            JOIN countries c  ON c.id  = ar.country_id
            JOIN platforms pl ON pl.id = ar.platform_id
            GROUP BY c.iso2, c.name_es, c.whr_rank_2025, ar.subindicator, pl.slug
            ORDER BY ABS(AVG(ar.gap)) DESC NULLS LAST
            LIMIT :top_n
        """), {"top_n": top_n}).fetchall()

    if not rows:
        print("  ⚠ No hay resultados calculados")
        return

    print(f"\n{'ISO2':6} {'País':18} {'WHR#':5} {'Subindicador':16} {'Platform':10} "
          f"{'Sentim.':9} {'WHR norm':9} {'GAP':9} {'Posts':6}")
    print("─" * 92)
    for r in rows:
        gap_str = f"{r.avg_gap:+.3f}" if r.avg_gap is not None else "  N/A "
        indicator = "↑" if (r.avg_gap or 0) > 0 else "↓"
        print(f"{r.iso2:6} {r.name_es:18} {r.whr_rank_2025 or '-':5} "
              f"{r.subindicator:16} {r.platform:10} "
              f"{r.avg_sentiment or 0:9.3f} {r.avg_whr_norm or 0:9.3f} "
              f"{gap_str} {indicator} {r.posts or 0:6}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Calcula resultados y gap WHR vs sentimiento")
    parser.add_argument("--country", default=None)
    parser.add_argument("--show-gaps", action="store_true", help="Mostrar tabla de brechas")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    if args.show_gaps:
        show_gaps(args.top)
    else:
        compute_results(args.country)
        if not args.country:
            show_gaps(args.top)
