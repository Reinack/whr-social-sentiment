"""
classifier/classify_posts.py
Clasifica posts usando Claude (Sonnet) por subindicador WHR, sentimiento e intensidad.
Procesa en lotes, respeta rate limits y registra todo en la tabla classifications.

Uso:
    python -m classifier.classify_posts --batch-size 50
    python -m classifier.classify_posts --country AR --batch-size 100
    python -m classifier.classify_posts --resume   # continúa desde donde quedó
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from sqlalchemy import text
from db import get_session

try:
    import anthropic
except ImportError:
    print("⚠ Instalar: pip install anthropic")
    sys.exit(1)

MODEL = "claude-sonnet-4-20250514"
CONFIDENCE_THRESHOLD = 0.7

SYSTEM_PROMPT = """Eres un clasificador de texto para investigación académica sobre bienestar y felicidad.
Se te dará una afirmación extraída de redes sociales junto con el país e idioma de origen.

Tu tarea es clasificarla según los subindicadores del World Happiness Report (WHR).

SUBINDICADORES disponibles:
- apoyo_social: menciona familia, amigos, soledad, comunidad, apoyo emocional
- libertad: menciona libertad, derechos, censura, autonomía, democracia, elecciones
- economia_pib: menciona trabajo, salario, inflación, pobreza, costo de vida, desempleo
- salud: menciona salud, hospitales, bienestar físico, esperanza de vida, medicina
- generosidad: menciona donaciones, voluntariado, solidaridad, ayuda comunitaria
- corrupcion: menciona corrupción, gobierno corrupto, impunidad, confianza institucional
- ninguno: no corresponde claramente a ningún subindicador

RESPONDE ÚNICAMENTE con JSON válido, sin texto adicional, sin backticks:
{
  "subindicador": "...",
  "sentimiento": "positivo|negativo|neutro",
  "intensidad": 1|2|3,
  "confianza": 0.0-1.0,
  "razon": "una frase corta explicando la clasificación"
}

INTENSIDAD:
1 = leve (mención casual, sin emoción fuerte)
2 = moderada (opinión clara)
3 = fuerte (emoción intensa, queja grave, celebración marcada)

CONFIANZA:
1.0 = clasificación muy clara
0.7-0.9 = bastante claro
0.5-0.7 = dudoso (mixto o ambiguo)
< 0.5 = muy incierto"""


def classify_batch(
    batch_size: int = 50,
    country_iso2: str | None = None,
    resume: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Clasifica posts pendientes en lotes.
    'Pendiente' = post sin clasificación en la tabla classifications.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    stats = {"classified": 0, "accepted": 0, "rejected": 0, "errors": 0}

    while True:
        # Traer siguiente lote de posts sin clasificar
        posts = _get_pending_posts(batch_size, country_iso2)
        if not posts:
            print("  ✓ No quedan posts pendientes")
            break

        print(f"\n  Procesando lote de {len(posts)} posts...")

        for post in posts:
            result = _classify_one(client, post)

            if result is None:
                stats["errors"] += 1
                continue

            if dry_run:
                print(f"  [{post['iso2']}] {post['body'][:60]}...")
                print(f"    → {result['subindicador']} / {result['sentimiento']} / conf={result['confianza']}")
                stats["classified"] += 1
                if result["confianza"] >= CONFIDENCE_THRESHOLD:
                    stats["accepted"] += 1
                else:
                    stats["rejected"] += 1
                continue

            _save_classification(post["post_id"], result)
            stats["classified"] += 1
            if result["confianza"] >= CONFIDENCE_THRESHOLD:
                stats["accepted"] += 1
            else:
                stats["rejected"] += 1

            # Rate limit: ~60 requests/min en Sonnet
            time.sleep(1.1)

        print(f"  Lote completado. Clasificados: {stats['classified']} "
              f"| Aceptados: {stats['accepted']} | Rechazados: {stats['rejected']}")

        if not resume:
            break

    return stats


def _classify_one(client, post: dict) -> dict | None:
    """Clasifica un post individual con Claude."""
    user_msg = (
        f"País: {post['country_name']} ({post['iso2']})\n"
        f"Idioma esperado: {post['lang_expected'] or 'desconocido'}\n"
        f"Red social: {post['platform']}\n\n"
        f"Texto:\n{post['body']}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text.strip()

        # Limpiar posibles backticks
        raw = raw.replace("```json", "").replace("```", "").strip()

        parsed = json.loads(raw)

        # Validar campos requeridos
        required = {"subindicador", "sentimiento", "intensidad", "confianza"}
        if not required.issubset(parsed.keys()):
            raise ValueError(f"Campos faltantes: {required - set(parsed.keys())}")

        # Validar valores
        valid_subs = {
            "apoyo_social", "libertad", "economia_pib",
            "salud", "generosidad", "corrupcion", "ninguno"
        }
        valid_sent = {"positivo", "negativo", "neutro"}

        if parsed["subindicador"] not in valid_subs:
            parsed["subindicador"] = "ninguno"
        if parsed["sentimiento"] not in valid_sent:
            parsed["sentimiento"] = "neutro"

        parsed["intensidad"]   = max(1, min(3, int(parsed["intensidad"])))
        parsed["confianza"]    = max(0.0, min(1.0, float(parsed["confianza"])))
        parsed["model_version"] = MODEL
        parsed["raw_response"] = raw

        return parsed

    except json.JSONDecodeError as e:
        print(f"    ✗ JSON inválido para post {post['post_id']}: {e}")
        return None
    except Exception as e:
        print(f"    ✗ Error clasificando post {post['post_id']}: {e}")
        return None


def _get_pending_posts(limit: int, country_iso2: str | None) -> list[dict]:
    """Retorna posts sin clasificación."""
    country_filter = "AND c.iso2 = :iso2" if country_iso2 else ""

    with get_session() as session:
        rows = session.execute(text(f"""
            SELECT
                p.id        AS post_id,
                p.body,
                p.lang_expected,
                p.sample_month,
                c.iso2,
                c.name_es   AS country_name,
                pl.slug     AS platform
            FROM posts p
            JOIN countries  c  ON c.id  = p.country_id
            JOIN platforms  pl ON pl.id = p.platform_id
            LEFT JOIN classifications cl ON cl.post_id = p.id
            WHERE cl.id IS NULL
              AND p.sampled = TRUE
              AND LENGTH(p.body) >= 50
              {country_filter}
            ORDER BY p.id
            LIMIT :limit
        """), {"limit": limit, "iso2": country_iso2}).fetchall()

    return [dict(r._mapping) for r in rows]


def _save_classification(post_id: int, result: dict):
    """Persiste la clasificación en la base."""
    with get_session() as session:
        session.execute(text("""
            INSERT INTO classifications
                (post_id, subindicator, sentiment, intensity,
                 confidence, model_version, raw_response)
            VALUES
                (:post_id, :subindicador, :sentimiento, :intensidad,
                 :confianza, :model_version, :raw_response::jsonb)
            ON CONFLICT (post_id) DO UPDATE SET
                subindicator  = EXCLUDED.subindicator,
                sentiment     = EXCLUDED.sentiment,
                intensity     = EXCLUDED.intensity,
                confidence    = EXCLUDED.confidence,
                model_version = EXCLUDED.model_version,
                raw_response  = EXCLUDED.raw_response,
                classified_at = NOW()
        """), {
            "post_id":       post_id,
            "subindicador":  result["subindicador"],
            "sentimiento":   result["sentimiento"],
            "intensidad":    result["intensidad"],
            "confianza":     result["confianza"],
            "model_version": result["model_version"],
            "raw_response":  result["raw_response"],
        })


def show_stats():
    """Muestra estadísticas de clasificación actuales."""
    with get_session() as session:
        rows = session.execute(text("""
            SELECT
                c.iso2,
                c.name_es,
                COUNT(p.id)                                         AS total_posts,
                COUNT(cl.id)                                        AS classified,
                COUNT(cl.id) FILTER (WHERE cl.accepted)             AS accepted,
                ROUND(AVG(cl.confidence) FILTER
                      (WHERE cl.id IS NOT NULL)::numeric, 3)        AS avg_confidence
            FROM posts p
            JOIN countries c ON c.id = p.country_id
            LEFT JOIN classifications cl ON cl.post_id = p.id
            WHERE p.sampled = TRUE
            GROUP BY c.iso2, c.name_es
            ORDER BY c.iso2
        """)).fetchall()

    print(f"\n{'ISO2':6} {'País':20} {'Posts':8} {'Clasif.':8} {'Aceptados':10} {'Conf. media':12}")
    print("-" * 68)
    for r in rows:
        print(f"{r.iso2:6} {r.name_es:20} {r.total_posts:8} "
              f"{r.classified or 0:8} {r.accepted or 0:10} "
              f"{r.avg_confidence or 'N/A':12}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clasifica posts con Claude IA")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--country", default=None, help="ISO2 para procesar solo un país")
    parser.add_argument("--resume", action="store_true", help="Procesar todos los lotes hasta terminar")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true", help="Mostrar estadísticas actuales")
    args = parser.parse_args()

    if args.stats:
        show_stats()
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("✗ ANTHROPIC_API_KEY no configurado")
            sys.exit(1)

        stats = classify_batch(
            batch_size=args.batch_size,
            country_iso2=args.country,
            resume=args.resume,
            dry_run=args.dry_run,
        )
        print(f"\n{'='*40}")
        print(f"Clasificados: {stats['classified']} | "
              f"Aceptados: {stats['accepted']} | "
              f"Rechazados: {stats['rejected']} | "
              f"Errores: {stats['errors']}")
