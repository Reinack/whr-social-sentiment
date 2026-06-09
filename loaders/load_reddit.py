"""
loaders/load_reddit.py
Carga comentarios de Reddit via Pullpush.io (mirror comunitario de Pushshift).
Sin descarga de dumps — consulta la API REST directamente.

API: https://api.pullpush.io/reddit/search/comment/
Sin auth requerida. Max 100 items/request. Paginación por timestamp.

Uso:
    python -m loaders.load_reddit --country AR --limit 400
    python -m loaders.load_reddit --country ALL --limit 300
    python -m loaders.load_reddit --list-subreddits
"""
import argparse
import os
import sys
import time
import random
from collections import defaultdict
from datetime import datetime, timezone

import truststore
truststore.inject_into_ssl()

import certifi
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from sqlalchemy import text
from db import get_session

# ── Configuración ──────────────────────────────────────────────────────────────
PULLPUSH_URL  = "https://api.pullpush.io/reddit/search/comment/"
PAGE_SIZE     = 100    # máximo permitido por la API
REQUEST_DELAY = 1.5    # segundos entre requests (respetar el servicio comunitario)
TIMEOUT       = 45     # segundos por request (Pullpush puede ser lento)

START_TS = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp())
END_TS   = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
MIN_CHARS = 50

# ── Subreddits por país ────────────────────────────────────────────────────────
SUBREDDITS = {
    "US": ["AskReddit", "usa", "AmericanPolitics"],
    "GB": ["unitedkingdom", "AskUK"],
    "CA": ["canada"],
    "AU": ["australia", "AusFinance"],
    "BR": ["brasil", "desabafos"],
    "IN": ["india", "indiasocial"],
    "MX": ["mexico"],
    "AR": ["argentina"],
    "DE": ["germany", "de"],
    "FR": ["france"],
    "PH": ["Philippines"],
    "JP": ["japan"],
    "ZA": ["southafrica"],
    "IT": ["italy"],
    "PL": ["polska"],
    "TR": ["Turkey", "turkiye"],
    "KR": ["korea"],
    "ID": [],   # Reddit bloqueado en Indonesia
    "VN": ["vietnam"],
}

LANG_MAP = {
    "brasil": "pt", "desabafos": "pt",
    "mexico": "es", "argentina": "es",
    "germany": "de", "de": "de",
    "france": "fr",
    "italy": "it",
    "polska": "pl",
    "Turkey": "tr", "turkiye": "tr",
    "korea": "ko",
    "vietnam": "vi",
}


def load_reddit(
    iso2: str,
    limit_per_country: int = 400,
    dry_run: bool = False,
) -> dict:
    """Recolecta comentarios de los subreddits de un país via Pullpush."""

    subreddits = SUBREDDITS.get(iso2.upper(), [])
    if not subreddits:
        print(f"  [!] {iso2}: sin subreddits configurados o Reddit bloqueado — omitido")
        return {"skipped": 1}

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Reddit -> {iso2}  ({', '.join('r/'+s for s in subreddits)})")

    with get_session() as session:
        country = session.execute(
            text("SELECT id FROM countries WHERE iso2 = :iso2"),
            {"iso2": iso2.upper()}
        ).fetchone()
        platform = session.execute(
            text("SELECT id FROM platforms WHERE slug = 'reddit'")
        ).fetchone()

    if not country or not platform:
        print("  [X] country o platform no encontrado en la base")
        return {"errors": 1}

    country_id  = country.id
    platform_id = platform.id

    # Recolectar candidatos de todos los subreddits
    max_candidates = limit_per_country * 8  # techo por subreddit para no paginar infinito
    candidates = []
    for sub in subreddits:
        print(f"  Consultando r/{sub}...")
        sub_candidates = _fetch_subreddit(sub, max_candidates=max_candidates)
        print(f"    {len(sub_candidates)} comentarios en ventana 2022-2024")
        candidates.extend(sub_candidates)

    if not candidates:
        print(f"  [X] No se encontraron candidatos para {iso2}")
        return {"no_data": 1}

    print(f"  Total candidatos: {len(candidates)}")

    # Muestreo estratificado por mes
    sampled = _stratified_sample(candidates, limit_per_country)
    print(f"  Seleccionados (muestreo estratificado): {len(sampled)}")

    if dry_run:
        for p in sampled[:3]:
            body_preview = p["body"][:80].encode("ascii", errors="replace").decode()
            print(f"    [{p['sample_month']}] {body_preview}...")
        return {"sampled": len(sampled)}

    # Insertar en base
    stats = {"inserted": 0, "errors": 0}
    with get_session() as session:
        for post in sampled:
            try:
                session.execute(text("""
                    INSERT INTO posts
                        (country_id, platform_id, body, lang_expected,
                         posted_at, source_id, source_type,
                         subreddit, sampled, sample_month)
                    VALUES
                        (:country_id, :platform_id, :body, :lang_expected,
                         :posted_at, :source_id, 'comment',
                         :subreddit, TRUE, :sample_month)
                    ON CONFLICT DO NOTHING
                """), {
                    "country_id":    country_id,
                    "platform_id":   platform_id,
                    "body":          post["body"],
                    "lang_expected": post["lang_expected"],
                    "posted_at":     post["posted_at"],
                    "source_id":     post["source_id"],
                    "subreddit":     post["subreddit"],
                    "sample_month":  post["sample_month"],
                })
                stats["inserted"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"    [X] Error insertando {post.get('source_id')}: {e}")

    print(f"  [OK] Insertados: {stats['inserted']} | Errores: {stats['errors']}")
    return stats


def _fetch_subreddit(subreddit: str, max_candidates: int = 2000) -> list[dict]:
    """
    Descarga comentarios de un subreddit via Pullpush paginando por timestamp.
    Retorna hasta max_candidates candidatos dentro de la ventana 2022-2024.
    """
    candidates = []
    before = END_TS
    lang   = LANG_MAP.get(subreddit, "en")
    consecutive_errors = 0

    while True:
        if len(candidates) >= max_candidates:
            print(f"    [~] Límite de {max_candidates} candidatos alcanzado, cortando paginación")
            break
        try:
            resp = requests.get(
                PULLPUSH_URL,
                params={
                    "subreddit": subreddit,
                    "after":     START_TS,
                    "before":    before,
                    "size":      PAGE_SIZE,
                    "sort":      "desc",
                },
                verify=certifi.where(),
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            items = resp.json().get("data", [])
            consecutive_errors = 0

        except requests.exceptions.Timeout:
            print(f"    [!] Timeout en r/{subreddit} — reintentando...")
            consecutive_errors += 1
            if consecutive_errors >= 3:
                print(f"    [!] 3 timeouts consecutivos, abandonando r/{subreddit}")
                break
            time.sleep(5)
            continue

        except Exception as e:
            print(f"    [!] Error en r/{subreddit}: {e}")
            consecutive_errors += 1
            if consecutive_errors >= 3:
                break
            time.sleep(5)
            continue

        if not items:
            break

        for item in items:
            body = item.get("body", "").strip()
            if not body or body in ("[deleted]", "[removed]") or len(body) < MIN_CHARS:
                continue
            created = item.get("created_utc", 0)
            if not (START_TS <= int(created) < END_TS):
                continue
            dt = datetime.fromtimestamp(int(created), tz=timezone.utc)
            candidates.append({
                "body":          body[:2000],
                "source_id":     item.get("id", ""),
                "posted_at":     dt.isoformat(),
                "sample_month":  dt.strftime("%Y-%m"),
                "subreddit":     item.get("subreddit", subreddit),
                "lang_expected": lang,
            })

        oldest = min(int(i.get("created_utc", END_TS)) for i in items)
        if oldest <= START_TS or len(items) < PAGE_SIZE:
            break

        before = oldest
        time.sleep(REQUEST_DELAY)

    return candidates


def _stratified_sample(candidates: list, limit: int) -> list:
    """Muestreo aleatorio estratificado por mes."""
    by_month = defaultdict(list)
    for c in candidates:
        by_month[c["sample_month"]].append(c)

    months = sorted(by_month.keys())
    if not months:
        return []

    per_month = max(1, limit // len(months))
    sampled   = []
    for month in months:
        pool = by_month[month]
        n    = min(per_month, len(pool))
        sampled.extend(random.sample(pool, n))

    remaining = limit - len(sampled)
    if remaining > 0:
        used      = set(id(c) for c in sampled)
        unsampled = [c for m in months for c in by_month[m] if id(c) not in used]
        sampled.extend(random.sample(unsampled, min(remaining, len(unsampled))))

    return sampled[:limit]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Carga Reddit via Pullpush.io API")
    parser.add_argument("--country", default="ALL",
                        help="ISO2 del país (ej: AR) o ALL para todos")
    parser.add_argument("--limit", type=int, default=400,
                        help="Posts por país (default 400)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-subreddits", action="store_true")
    args = parser.parse_args()

    if args.list_subreddits:
        print("\nSubreddits configurados por país:")
        for iso2, subs in SUBREDDITS.items():
            status = "[!] Reddit bloqueado" if not subs else ", ".join(f"r/{s}" for s in subs)
            print(f"  {iso2}: {status}")
        sys.exit(0)

    countries = list(SUBREDDITS.keys()) if args.country == "ALL" \
                else [args.country.upper()]

    total = {"inserted": 0, "errors": 0}
    for iso2 in countries:
        stats = load_reddit(iso2, args.limit, args.dry_run)
        total["inserted"] += stats.get("inserted", 0)
        total["errors"]   += stats.get("errors", 0)

    print(f"\n{'='*40}")
    print(f"Total insertados: {total['inserted']} | Errores: {total['errors']}")
