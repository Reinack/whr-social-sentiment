"""
classifier/classify_posts.py
Clasifica posts usando Groq (llama3) por subindicador WHR, sentimiento e intensidad.
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

PID_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "classifier.pid")


def _write_pid(model: str, batch_size: int, resume: bool):
    with open(PID_FILE, "w") as f:
        f.write(f"pid={os.getpid()}\n")
        f.write(f"started={datetime.now().isoformat()}\n")
        f.write(f"model={model}\n")
        f.write(f"batch_size={batch_size}\n")
        f.write(f"resume={resume}\n")


def _update_pid(model: str, classified: int):
    try:
        if not os.path.exists(PID_FILE):
            return
        with open(PID_FILE, "r") as f:
            lines = f.readlines()
        data = {l.split("=")[0]: l.split("=",1)[1].strip() for l in lines if "=" in l}
        data["model"] = model
        data["classified_this_run"] = str(classified)
        data["last_update"] = datetime.now().isoformat()
        with open(PID_FILE, "w") as f:
            for k, v in data.items():
                f.write(f"{k}={v}\n")
    except OSError:
        pass  # no crítico, ignorar errores de I/O en el PID file


def _remove_pid():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # truststore opcional, solo necesario en Windows con certs corporativos

try:
    from groq import Groq
except ImportError:
    print("[!] Instalar: pip install groq")
    sys.exit(1)

# Modelos en orden de preferencia por key.
MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "gemma2-9b-it",
    "llama3-8b-8192",
]
CONFIDENCE_THRESHOLD = 0.7

SYSTEM_PROMPT = """Clasificador WHR. Responde SOLO JSON sin backticks.
Subindicadores: apoyo_social|libertad|economia_pib|salud|generosidad|corrupcion|ninguno
Sentimiento: positivo|negativo|neutro. Intensidad: 1=leve,2=moderada,3=fuerte. Confianza: 0.0-1.0.
{"subindicador":"...","sentimiento":"...","intensidad":1,"confianza":0.0,"razon":"..."}"""


def classify_batch(
    batch_size: int = 50,
    country_iso2: str | None = None,
    resume: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Clasifica posts pendientes en lotes.
    Rota modelos automáticamente cuando uno alcanza su límite diario.
    """
    # Guardia: no lanzar si ya hay un proceso corriendo
    if os.path.exists(PID_FILE):
        data = {l.split("=")[0]: l.split("=",1)[1].strip()
                for l in open(PID_FILE) if "=" in l}
        existing_pid = int(data.get("pid", 0))
        try:
            os.kill(existing_pid, 0)  # 0 = solo verificar, no matar
            print(f"[!] Ya hay un clasificador corriendo (PID {existing_pid}, "
                  f"modelo {data.get('model','?')}, "
                  f"inicio {data.get('started','?')[:19].replace('T',' ')})")
            print(f"    Si el proceso murió, eliminá classifier.pid y reintentá.")
            sys.exit(1)
        except (OSError, ProcessLookupError):
            # PID no existe, el archivo es huérfano — lo limpiamos
            os.remove(PID_FILE)

    # Construir lista de (client, model) combinando todas las keys × modelos
    # Lee GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3, ... automáticamente
    api_keys = [k for k in [
        os.getenv("GROQ_API_KEY"),
        *[os.getenv(f"GROQ_API_KEY_{i}") for i in range(2, 10)]
    ] if k]
    slots = [(Groq(api_key=k), m) for k in api_keys for m in MODELS]
    slot_idx = 0

    stats = {"classified": 0, "accepted": 0, "rejected": 0, "errors": 0}

    _write_pid(slots[0][1], batch_size, resume)

    try:
        while True:
            if slot_idx >= len(slots):
                print("  [!] Todos los modelos y keys alcanzaron su límite diario. Reintentar mañana.")
                break

            client, model = slots[slot_idx]
            _update_pid(model, stats["classified"])

            posts = _get_pending_posts(batch_size, country_iso2)
            if not posts:
                print("  [OK] No quedan posts pendientes")
                break

            print(f"\n  Procesando lote de {len(posts)} posts... [modelo: {model}]")

            quota_hit = False
            for post in posts:
                result = _classify_one(client, post, model)

                if result == "QUOTA":
                    key_num = (slot_idx // len(MODELS)) + 1
                    print(f"  [~] Límite diario de key{key_num}/{model}, rotando al siguiente slot...")
                    slot_idx += 1
                    quota_hit = True
                    break

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

                # Rate limit: 30 RPM en free tier de Groq
                time.sleep(2.1)

            if not quota_hit:
                print(f"  Lote completado. Clasificados: {stats['classified']} "
                      f"| Aceptados: {stats['accepted']} | Rechazados: {stats['rejected']}")

            if not resume and not quota_hit:
                break

    finally:
        _remove_pid()

    return stats


def _classify_one(client, post: dict, model: str) -> dict | None | str:
    """
    Clasifica un post individual con Groq.
    Retorna "QUOTA" si se alcanzó el límite diario del modelo.
    """
    user_msg = (
        f"País: {post['country_name']} ({post['iso2']}). "
        f"Red: {post['platform']}.\n{post['body']}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=128,
            temperature=0.1,
        )

        raw = response.choices[0].message.content.strip()

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
        parsed["model_version"] = model
        parsed["raw_response"] = raw

        return parsed

    except json.JSONDecodeError as e:
        print(f"    [X] JSON inválido para post {post['post_id']}: {e} — guardando fallback")
        return {
            "subindicador": "ninguno", "sentimiento": "neutro",
            "intensidad": 1, "confianza": 0.0,
            "razon": f"parse_error: {str(e)[:80]}",
            "model_version": model, "raw_response": '{"error":"json_parse"}',
        }
    except Exception as e:
        msg = str(e)
        if "429" in msg and ("TPD" in msg or "per day" in msg or "quota" in msg.lower()):
            return "QUOTA"
        if "decommissioned" in msg or "model_decommissioned" in msg:
            return "QUOTA"  # tratar como quota para rotar al siguiente slot
        print(f"    [X] Error clasificando post {post['post_id']}: {e}")
        # Respuesta vacía u otro error de API — guardar fallback para no reintentar
        if not msg or "content" in msg.lower() or len(msg) < 20:
            return {
                "subindicador": "ninguno", "sentimiento": "neutro",
                "intensidad": 1, "confianza": 0.0,
                "razon": "api_error: empty_response",
                "model_version": model, "raw_response": '{"error":"api_error"}',
            }
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
                 :confianza, :model_version, CAST(:raw_response AS jsonb))
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
        if not os.getenv("GROQ_API_KEY"):
            print("[X] GROQ_API_KEY no configurado")
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

