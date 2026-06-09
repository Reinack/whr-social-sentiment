from db import get_session
from sqlalchemy import text

with get_session() as s:
    # Países con YouTube
    yt = s.execute(text("""
        SELECT c.name, COUNT(*) as posts
        FROM posts p JOIN countries c ON c.id = p.country_id
        WHERE p.source_type = 'video_comment'
        GROUP BY c.name ORDER BY c.name
    """)).fetchall()
    print(f"YouTube ({len(yt)} countries):")
    for r in yt:
        print(f"  {r[0]}: {r[1]}")

    # Países con Reddit
    rd = s.execute(text("""
        SELECT c.name, COUNT(*) as posts
        FROM posts p JOIN countries c ON c.id = p.country_id
        WHERE p.source_type = 'comment'
        GROUP BY c.name ORDER BY c.name
    """)).fetchall()
    print(f"\nReddit ({len(rd)} countries):")
    for r in rd:
        print(f"  {r[0]}: {r[1]}")

    # Países sin datos de ninguna fuente
    missing = s.execute(text("""
        SELECT c.name FROM countries c
        WHERE c.id NOT IN (SELECT DISTINCT country_id FROM posts)
        ORDER BY c.name
    """)).fetchall()
    print(f"\nSin ningún post ({len(missing)} countries):")
    for r in missing:
        print(f"  {r[0]}")
