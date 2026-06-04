"""
run_pipeline.py
Orquestador principal del pipeline completo.
Ejecuta cada fase en orden con logging y manejo de errores.

Uso:
    python run_pipeline.py --step whr          # solo carga WHR
    python run_pipeline.py --step reddit       # solo Reddit
    python run_pipeline.py --step youtube      # solo YouTube
    python run_pipeline.py --step tsgi         # solo TSGI MIT/Harvard
    python run_pipeline.py --step classify     # solo clasificación
    python run_pipeline.py --step analyze      # solo análisis
    python run_pipeline.py --all               # todo el pipeline
    python run_pipeline.py --status            # estado actual
"""
import argparse
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Importar módulos del pipeline ─────────────────────────────
from loaders.load_whr     import load_whr, verify_load
from loaders.load_reddit  import load_reddit, SUBREDDITS
from loaders.load_youtube import load_youtube, YOUTUBE_REGIONS
from loaders.load_tsgi    import load_tsgi, verify_load as verify_tsgi
from classifier.classify_posts import classify_batch, show_stats
from analysis.compute_results  import compute_results, show_gaps
from db import get_session
from sqlalchemy import text

# ── Configuración ─────────────────────────────────────────────
COUNTRIES = [
    "US","GB","CA","AU","BR","IN","MX","AR","DE","FR",
    "PH","JP","ZA","IT","PL","TR","KR","ID","VN"
]


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def step_whr():
    log("━━ FASE 1: Carga WHR Excel ━━")
    path = os.getenv("WHR_EXCEL_PATH", "data/WHR26_Data_Figure_2_1.xlsx")
    if not os.path.exists(path):
        log(f"✗ Archivo no encontrado: {path}")
        log("  Copiar el Excel WHR a data/ o ajustar WHR_EXCEL_PATH en .env")
        return False
    stats = load_whr(path)
    log(f"  ✓ WHR cargado — {stats['inserted']} insertados, {stats['updated']} actualizados")
    return True


def step_reddit():
    log("━━ FASE 2a: Carga Reddit (Pushshift) ━━")
    dumps_dir = os.getenv("REDDIT_DUMPS_DIR", "data/reddit/")
    limit     = int(os.getenv("REDDIT_LIMIT", "400"))

    if not os.path.isdir(dumps_dir):
        log(f"✗ Directorio no encontrado: {dumps_dir}")
        log("  Descargar dumps de https://academictorrents.com y colocar en data/reddit/")
        return False

    total_inserted = 0
    for iso2 in COUNTRIES:
        if not SUBREDDITS.get(iso2):
            log(f"  {iso2}: Reddit no disponible — omitido")
            continue
        stats = load_reddit(iso2, dumps_dir, limit)
        total_inserted += stats.get("inserted", 0)

    log(f"  ✓ Reddit — {total_inserted} posts insertados en total")
    return True


def step_youtube():
    log("━━ FASE 2b: Recolección YouTube ━━")
    api_key = os.getenv("YOUTUBE_API_KEY")
    limit   = int(os.getenv("YOUTUBE_LIMIT", "400"))

    if not api_key:
        log("✗ YOUTUBE_API_KEY no configurado en .env")
        return False

    total_inserted = 0
    for iso2 in COUNTRIES:
        stats = load_youtube(iso2, api_key, limit)
        total_inserted += stats.get("inserted", 0)

    log(f"  ✓ YouTube — {total_inserted} comentarios insertados en total")
    return True


def step_tsgi():
    log("━━ FASE 2c: Carga TSGI MIT/Harvard ━━")
    tsgi_path = os.getenv("TSGI_PATH", "data/tsgi/")

    if not os.path.exists(tsgi_path):
        log(f"✗ Ruta no encontrada: {tsgi_path}")
        log("  Descargar desde https://doi.org/10.7910/DVN/3IL00Q")
        log("  Colocar los CSV en data/tsgi/ o ajustar TSGI_PATH en .env")
        return False

    stats = load_tsgi(tsgi_path)
    log(f"  ✓ TSGI — {stats.get('inserted', 0)} insertados | {stats.get('updated', 0)} actualizados")
    log("  ℹ Dataset cubre hasta 2023. 2024 sin benchmark TSGI (limitación documentada)")
    return True


def step_classify():
    log("━━ FASE 3: Clasificación IA (Claude) ━━")
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        log("✗ ANTHROPIC_API_KEY no configurado en .env")
        return False

    batch_size = int(os.getenv("CLASSIFY_BATCH_SIZE", "50"))
    stats = classify_batch(batch_size=batch_size, resume=True)

    log(f"  ✓ Clasificación — {stats['classified']} procesados "
        f"| {stats['accepted']} aceptados "
        f"| {stats['rejected']} rechazados")
    return True


def step_analyze():
    log("━━ FASE 4: Análisis y cálculo de gaps WHR ━━")
    stats = compute_results()
    log(f"  ✓ Análisis — {stats['rows']} filas en analysis_results")
    return True


def show_status():
    """Muestra el estado actual del pipeline."""
    log("━━ ESTADO DEL PIPELINE ━━")
    with get_session() as session:
        counts = session.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM countries)          AS countries,
                (SELECT COUNT(*) FROM whr_scores)         AS whr_scores,
                (SELECT COUNT(*) FROM posts)              AS posts,
                (SELECT COUNT(*) FROM posts
                 WHERE sampled = TRUE)                    AS posts_sampled,
                (SELECT COUNT(*) FROM classifications)    AS classifications,
                (SELECT COUNT(*) FROM classifications
                 WHERE accepted = TRUE)                   AS accepted,
                (SELECT COUNT(*) FROM analysis_results)  AS results,
                (SELECT COUNT(*) FROM tsgi_index)         AS tsgi_rows
        """)).fetchone()

    print(f"""
  Países cargados:        {counts.countries}
  Scores WHR:             {counts.whr_scores}
  Posts totales:          {counts.posts}
  Posts muestreados:      {counts.posts_sampled}
  Clasificaciones totales:{counts.classifications}
  Clasificaciones aceptadas: {counts.accepted}
  Resultados de análisis: {counts.results}
  Filas TSGI:             {counts.tsgi_rows}
    """)

    if counts.accepted > 0:
        print("  Preview gaps (top 5):")
        show_gaps(top_n=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipeline WHR × Redes Sociales")
    parser.add_argument("--step",
        choices=["whr","reddit","youtube","tsgi","classify","analyze"],
        help="Ejecutar solo una fase")
    parser.add_argument("--all", action="store_true",
        help="Ejecutar pipeline completo")
    parser.add_argument("--status", action="store_true",
        help="Mostrar estado actual")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.all:
        log("▶ Pipeline completo")
        step_whr()
        step_reddit()
        step_youtube()
        step_tsgi()
        step_classify()
        step_analyze()
        show_status()
    elif args.step == "whr":
        step_whr()
    elif args.step == "reddit":
        step_reddit()
    elif args.step == "youtube":
        step_youtube()
    elif args.step == "tsgi":
        step_tsgi()
    elif args.step == "classify":
        step_classify()
    elif args.step == "analyze":
        step_analyze()
    else:
        parser.print_help()
