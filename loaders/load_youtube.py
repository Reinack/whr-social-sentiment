"""
loaders/load_youtube.py
Recolecta comentarios de videos trending por país usando YouTube Data API v3.
Es la fuente primaria para Indonesia, Corea del Sur y Vietnam.

Uso:
    python -m loaders.load_youtube --country ID --limit 400
    python -m loaders.load_youtube --country ALL --limit 300
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta

import truststore
truststore.inject_into_ssl()

import certifi
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from sqlalchemy import text
from db import get_session

YT_SEARCH_URL   = "https://www.googleapis.com/youtube/v3/search"
YT_COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"

def _yt_get(url: str, params: dict) -> dict:
    """GET a la API de YouTube con certifi y manejo de errores."""
    resp = requests.get(url, params=params, verify=certifi.where(), timeout=15)
    resp.raise_for_status()
    return resp.json()

# Ventana temporal
START_DATE = datetime(2022, 1, 1, tzinfo=timezone.utc)
END_DATE   = datetime(2025, 1, 1, tzinfo=timezone.utc)

# Región ISO2 -> código de región YouTube + idioma esperado
YOUTUBE_REGIONS = {
    "US": ("US", "en"),  "GB": ("GB", "en"),  "CA": ("CA", "en"),
    "AU": ("AU", "en"),  "BR": ("BR", "pt"),  "IN": ("IN", "en"),
    "MX": ("MX", "es"),  "AR": ("AR", "es"),  "DE": ("DE", "de"),
    "FR": ("FR", "fr"),  "PH": ("PH", "en"),  "JP": ("JP", "ja"),
    "ZA": ("ZA", "en"),  "IT": ("IT", "it"),  "PL": ("PL", "pl"),
    "TR": ("TR", "tr"),  "KR": ("KR", "ko"),  "ID": ("ID", "id"),
    "VN": ("VN", "vi"),
}

# Términos de búsqueda por subindicador (plantilla — se adapta por idioma)
# Para esta primera versión usamos términos generales en el idioma del país
# El clasificador IA los mapeará al subindicador correcto después
SEARCH_TERMS = {
    "en": "life happiness wellbeing society government corruption",
    "es": "felicidad bienestar sociedad gobierno corrupción vida",
    "pt": "felicidade bem-estar sociedade governo corrupção vida",
    "de": "Glück Wohlbefinden Gesellschaft Regierung Korruption Leben",
    "fr": "bonheur bien-être société gouvernement corruption vie",
    "it": "felicità benessere società governo corruzione vita",
    "pl": "szczęście dobrobyt społeczeństwo rząd korupcja życie",
    "tr": "mutluluk refah toplum hükümet yolsuzluk yaşam",
    "ko": "행복 복지 사회 정부 부패 삶",
    "id": "kebahagiaan kesejahteraan masyarakat pemerintah korupsi kehidupan",
    "vi": "hạnh phúc phúc lợi xã hội chính phủ tham nhũng cuộc sống",
    "ja": "幸福 福祉 社会 政府 腐敗 生活",
}

MIN_COMMENT_LEN = 40


def load_youtube(
    iso2: str,
    api_key: str,
    limit_per_country: int = 400,
    dry_run: bool = False,
) -> dict:
    """Recolecta comentarios de YouTube para un país dado."""

    if iso2.upper() not in YOUTUBE_REGIONS:
        print(f"  [!] {iso2}: no configurado")
        return {"skipped": 1}

    region_code, lang = YOUTUBE_REGIONS[iso2.upper()]
    search_q = SEARCH_TERMS.get(lang, SEARCH_TERMS["en"])

    print(f"\n{'[DRY RUN] ' if dry_run else ''}YouTube -> {iso2} ({region_code}, {lang})")

    # Obtener IDs de base
    with get_session() as session:
        country = session.execute(
            text("SELECT id FROM countries WHERE iso2 = :iso2"),
            {"iso2": iso2.upper()}
        ).fetchone()
        platform = session.execute(
            text("SELECT id FROM platforms WHERE slug = 'youtube'")
        ).fetchone()

    if not country or not platform:
        print("  [X] country o platform no encontrado")
        return {"errors": 1}

    country_id  = country.id
    platform_id = platform.id

    # Recolectar video IDs por trimestre (para cubrir 2022-2024 uniformemente)
    video_ids = _get_video_ids(api_key, region_code, search_q)
    print(f"  Videos encontrados: {len(video_ids)}")

    if not video_ids:
        print("  [X] No se encontraron videos")
        return {"no_data": 1}

    # Recolectar comentarios de los videos
    comments = _get_comments(api_key, video_ids, lang, limit_per_country * 3)
    print(f"  Comentarios candidatos: {len(comments)}")

    # Muestreo estratificado por mes
    from loaders.load_reddit import _stratified_sample
    sampled = _stratified_sample(comments, limit_per_country)
    print(f"  Seleccionados (muestreo): {len(sampled)}")

    if dry_run:
        for c in sampled[:3]:
            print(f"    [{c['sample_month']}] {c['body'][:80]}...")
        return {"sampled": len(sampled)}

    # Insertar en base
    stats = {"inserted": 0, "errors": 0}

    with get_session() as session:
        for comment in sampled:
            try:
                session.execute(text("""
                    INSERT INTO posts
                        (country_id, platform_id, body, lang_expected,
                         posted_at, source_id, source_type,
                         channel_id, sampled, sample_month)
                    VALUES
                        (:country_id, :platform_id, :body, :lang_expected,
                         :posted_at, :source_id, 'video_comment',
                         :channel_id, TRUE, :sample_month)
                    ON CONFLICT DO NOTHING
                """), {
                    "country_id":   country_id,
                    "platform_id":  platform_id,
                    "body":         comment["body"],
                    "lang_expected": lang,
                    "posted_at":    comment["posted_at"],
                    "source_id":    comment["source_id"],
                    "channel_id":   comment.get("channel_id"),
                    "sample_month": comment["sample_month"],
                })
                stats["inserted"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"    [X] {e}")

    print(f"  [OK] Insertados: {stats['inserted']} | Errores: {stats['errors']}")
    return stats


def _get_video_ids(api_key: str, region_code: str, query: str) -> list[str]:
    """Busca videos relevantes para la región usando requests."""
    video_ids = []
    current = START_DATE
    while current < END_DATE:
        end = min(current + relativedelta(months=3), END_DATE)
        try:
            data = _yt_get(YT_SEARCH_URL, {
                "part":            "id",
                "q":               query,
                "type":            "video",
                "regionCode":      region_code,
                "relevanceLanguage": region_code.lower(),
                "publishedAfter":  current.strftime("%Y-%m-%dT00:00:00Z"),
                "publishedBefore": end.strftime("%Y-%m-%dT00:00:00Z"),
                "maxResults":      10,
                "order":           "relevance",
                "key":             api_key,
            })
            for item in data.get("items", []):
                vid_id = item.get("id", {}).get("videoId")
                if vid_id:
                    video_ids.append(vid_id)
            time.sleep(0.5)
        except Exception as e:
            print(f"    [!] Error en {current.strftime('%Y-%m')}: {e}")
        current = end
    return list(set(video_ids))


def _get_comments(api_key: str, video_ids: list[str], lang: str, max_total: int) -> list[dict]:
    """Recolecta comentarios de una lista de videos usando requests."""
    comments = []
    for video_id in video_ids:
        if len(comments) >= max_total:
            break
        try:
            page_token = None
            while len(comments) < max_total:
                params = {
                    "part":        "snippet",
                    "videoId":     video_id,
                    "maxResults":  100,
                    "textFormat":  "plainText",
                    "order":       "relevance",
                    "key":         api_key,
                }
                if page_token:
                    params["pageToken"] = page_token

                data = _yt_get(YT_COMMENTS_URL, params)

                for item in data.get("items", []):
                    snippet = item["snippet"]["topLevelComment"]["snippet"]
                    body = snippet.get("textDisplay", "").strip()
                    if len(body) < MIN_COMMENT_LEN:
                        continue
                    published = snippet.get("publishedAt", "")
                    try:
                        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                        if not (START_DATE <= dt < END_DATE):
                            continue
                    except ValueError:
                        continue
                    comments.append({
                        "body":          body[:2000],
                        "source_id":     item["id"],
                        "posted_at":     dt.isoformat(),
                        "sample_month":  dt.strftime("%Y-%m"),
                        "channel_id":    item["snippet"].get("channelId"),
                        "lang_expected": lang,
                    })

                page_token = data.get("nextPageToken")
                if not page_token:
                    break
                time.sleep(0.3)

        except requests.HTTPError as e:
            if e.response is not None and "commentsDisabled" in e.response.text:
                pass
            else:
                print(f"    [!] Error en video {video_id}: {e}")
        except Exception as e:
            print(f"    [!] Error en video {video_id}: {e}")

    return comments


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Carga comentarios YouTube por país")
    parser.add_argument("--country", default="ALL")
    parser.add_argument("--api-key", default=os.getenv("YOUTUBE_API_KEY"))
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        print("[X] YOUTUBE_API_KEY no configurado")
        sys.exit(1)

    countries = list(YOUTUBE_REGIONS.keys()) if args.country == "ALL" \
                else [args.country.upper()]

    total = {"inserted": 0, "errors": 0}
    for iso2 in countries:
        stats = load_youtube(iso2, args.api_key, args.limit, args.dry_run)
        total["inserted"] += stats.get("inserted", 0)
        total["errors"]   += stats.get("errors", 0)

    print(f"\n{'='*40}")
    print(f"Total insertados: {total['inserted']} | Errores: {total['errors']}")


