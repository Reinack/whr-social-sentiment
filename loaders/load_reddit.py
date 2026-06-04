"""
loaders/load_reddit.py
Carga dumps de Pushshift Reddit (ndjson.zst) a la tabla posts.
Filtra por subreddit de país, ventana 2022-2024, y muestra estratificado por mes.

Uso:
    python -m loaders.load_reddit --country AR --limit 400
    python -m loaders.load_reddit --country ALL --limit 300
    python -m loaders.load_reddit --list-subreddits
"""
import argparse
import gzip
import json
import os
import random
import sys
import zstandard as zstd
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from sqlalchemy import text
from db import get_session

# Ventana temporal
START_TS = datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp()
END_TS   = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()

# Subreddits por iso2
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
    # Indonesia y Vietnam: Reddit limitado
    "ID": [],  # Reddit bloqueado en Indonesia
    "VN": ["vietnam"],
}

# Longitud mínima de texto para clasificación
MIN_CHARS = 50


def load_reddit(
    iso2: str,
    dumps_dir: str,
    limit_per_country: int = 400,
    dry_run: bool = False,
) -> dict:
    """
    Lee archivos .zst o .ndjson del directorio dumps_dir,
    filtra por subreddit y ventana temporal, y carga en posts.
    """
    subreddits = SUBREDDITS.get(iso2.upper(), [])
    if not subreddits:
        print(f"  ⚠ {iso2}: sin subreddits configurados o Reddit bloqueado — omitido")
        return {"skipped": 1}

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Cargando Reddit para {iso2}")
    print(f"  Subreddits: {subreddits}")
    print(f"  Límite: {limit_per_country} posts")

    # Obtener country_id y platform_id
    with get_session() as session:
        country = session.execute(
            text("SELECT id FROM countries WHERE iso2 = :iso2"),
            {"iso2": iso2.upper()}
        ).fetchone()
        platform = session.execute(
            text("SELECT id FROM platforms WHERE slug = 'reddit'")
        ).fetchone()

    if not country or not platform:
        print(f"  ✗ country o platform no encontrado en la base")
        return {"errors": 1}

    country_id  = country.id
    platform_id = platform.id

    # Recolectar candidatos de los archivos dump
    candidates = []

    for sub in subreddits:
        # Buscar archivo dump: puede ser subreddit.zst, subreddit.ndjson.gz, etc.
        patterns = [
            f"{sub}.zst", f"{sub}.ndjson.zst",
            f"{sub}.ndjson.gz", f"{sub}.ndjson",
            f"RS_{sub}.zst",  # formato Pushshift comments
        ]
        filepath = None
        for pat in patterns:
            candidate = Path(dumps_dir) / pat
            if candidate.exists():
                filepath = candidate
                break

        if not filepath:
            print(f"  ⚠ Archivo no encontrado para r/{sub} en {dumps_dir}")
            continue

        print(f"  Leyendo r/{sub} desde {filepath.name}...")
        candidates.extend(_read_dump(filepath, sub))

    if not candidates:
        print(f"  ✗ No se encontraron candidatos para {iso2}")
        return {"no_data": 1}

    print(f"  Candidatos en ventana 2022-2024: {len(candidates)}")

    # Muestreo estratificado por mes (target: limit_per_country en total)
    sampled = _stratified_sample(candidates, limit_per_country)
    print(f"  Posts seleccionados (muestreo estratificado): {len(sampled)}")

    if dry_run:
        for p in sampled[:3]:
            print(f"    [{p['sample_month']}] {p['body'][:80]}...")
        return {"sampled": len(sampled)}

    # Insertar en base
    stats = {"inserted": 0, "skipped_duplicate": 0, "errors": 0}

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
                         :posted_at, :source_id, :source_type,
                         :subreddit, TRUE, :sample_month)
                    ON CONFLICT DO NOTHING
                """), {
                    "country_id":   country_id,
                    "platform_id":  platform_id,
                    "body":         post["body"],
                    "lang_expected": post["lang_expected"],
                    "posted_at":    post["posted_at"],
                    "source_id":    post["source_id"],
                    "source_type":  "comment",
                    "subreddit":    post["subreddit"],
                    "sample_month": post["sample_month"],
                })
                stats["inserted"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"    ✗ Error insertando {post.get('source_id')}: {e}")

    print(f"  ✓ Insertados: {stats['inserted']} | Errores: {stats['errors']}")
    return stats


def _read_dump(filepath: Path, subreddit: str) -> list[dict]:
    """Lee un archivo dump y retorna lista de candidatos en ventana temporal."""
    candidates = []
    suffix = filepath.suffix.lower()

    def process_line(line: str):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return

        # Aceptar posts (selftext) o comentarios (body)
        body = obj.get("body") or obj.get("selftext") or ""
        if not body or body in ("[deleted]", "[removed]") or len(body) < MIN_CHARS:
            return

        created = obj.get("created_utc")
        if not created or not (START_TS <= float(created) < END_TS):
            return

        dt = datetime.fromtimestamp(float(created), tz=timezone.utc)
        candidates.append({
            "body":         body[:2000],  # truncar textos muy largos
            "source_id":    obj.get("id", ""),
            "posted_at":    dt.isoformat(),
            "sample_month": dt.strftime("%Y-%m"),
            "subreddit":    obj.get("subreddit", subreddit),
            "lang_expected": _infer_lang(subreddit),
        })

    try:
        if suffix == ".zst":
            dctx = zstd.ZstdDecompressor()
            with open(filepath, "rb") as fh:
                with dctx.stream_reader(fh) as reader:
                    buffer = b""
                    while True:
                        chunk = reader.read(2**20)  # 1MB chunks
                        if not chunk:
                            break
                        buffer += chunk
                        lines = buffer.split(b"\n")
                        buffer = lines[-1]
                        for line in lines[:-1]:
                            if line.strip():
                                process_line(line.decode("utf-8", errors="ignore"))
        elif suffix in (".gz",):
            with gzip.open(filepath, "rt", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    process_line(line)
        else:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    process_line(line)
    except Exception as e:
        print(f"    ✗ Error leyendo {filepath.name}: {e}")

    return candidates


def _stratified_sample(candidates: list, limit: int) -> list:
    """Muestreo aleatorio estratificado por mes."""
    from collections import defaultdict
    by_month = defaultdict(list)
    for c in candidates:
        by_month[c["sample_month"]].append(c)

    months = sorted(by_month.keys())
    if not months:
        return []

    per_month = max(1, limit // len(months))
    sampled = []
    for month in months:
        pool = by_month[month]
        n = min(per_month, len(pool))
        sampled.extend(random.sample(pool, n))

    # Si hay cupo restante, completar aleatoriamente
    remaining = limit - len(sampled)
    if remaining > 0:
        all_unsampled = [c for m in months for c in by_month[m]
                         if c not in sampled]
        extra = random.sample(all_unsampled, min(remaining, len(all_unsampled)))
        sampled.extend(extra)

    return sampled[:limit]


def _infer_lang(subreddit: str) -> str:
    """Idioma esperado según el subreddit."""
    lang_map = {
        "brasil": "pt", "desabafos": "pt",
        "mexico": "es", "argentina": "es", "spain": "es",
        "germany": "de", "de": "de",
        "france": "fr",
        "italy": "it",
        "polska": "pl",
        "Turkey": "tr", "turkiye": "tr",
        "korea": "ko",
        "vietnam": "vi",
    }
    return lang_map.get(subreddit, "en")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Carga Reddit Pushshift dumps")
    parser.add_argument("--country", default="ALL",
                        help="ISO2 del país (ej: AR) o ALL para todos")
    parser.add_argument("--dumps-dir", default=os.getenv("REDDIT_DUMPS_DIR", "data/reddit/"))
    parser.add_argument("--limit", type=int, default=400,
                        help="Posts por país (default 400)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-subreddits", action="store_true")
    args = parser.parse_args()

    if args.list_subreddits:
        print("\nSubreddits configurados por país:")
        for iso2, subs in SUBREDDITS.items():
            status = "⚠ Reddit bloqueado" if not subs else ", ".join(f"r/{s}" for s in subs)
            print(f"  {iso2}: {status}")
        sys.exit(0)

    countries = list(SUBREDDITS.keys()) if args.country == "ALL" else [args.country.upper()]
    total = {"inserted": 0, "errors": 0}

    for iso2 in countries:
        stats = load_reddit(iso2, args.dumps_dir, args.limit, args.dry_run)
        total["inserted"] += stats.get("inserted", 0)
        total["errors"]   += stats.get("errors", 0)

    print(f"\n{'='*40}")
    print(f"Total insertados: {total['inserted']} | Errores: {total['errors']}")
