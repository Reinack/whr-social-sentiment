-- ============================================================
-- World Happiness Report x Social Media Analysis
-- Schema PostgreSQL — ventana temporal 2022-2024
-- ============================================================

-- Extensiones
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- búsqueda de texto

-- ============================================================
-- TIPOS ENUMERADOS
-- ============================================================

CREATE TYPE subindicator_type AS ENUM (
    'apoyo_social',
    'libertad',
    'economia_pib',
    'salud',
    'generosidad',
    'corrupcion',
    'ninguno'
);

CREATE TYPE sentiment_type AS ENUM (
    'positivo',
    'negativo',
    'neutro'
);

CREATE TYPE platform_api_type AS ENUM (
    'push',        -- API activa (YouTube)
    'pull',        -- dataset descargado (Pushshift, TSGI)
    'dataset',     -- archivo estático
    'scraping'     -- scraping ético documentado
);

CREATE TYPE post_source_type AS ENUM (
    'comment',
    'post',
    'tweet',
    'video_comment',
    'page_post'
);

CREATE TYPE tier_type AS ENUM ('T1', 'T2');

-- ============================================================
-- 1. COUNTRIES
-- ============================================================

CREATE TABLE countries (
    id                  SERIAL PRIMARY KEY,
    iso2                CHAR(2)      NOT NULL UNIQUE,   -- AR, BR, US...
    iso3                CHAR(3)      UNIQUE,            -- ARG, BRA, USA...
    name                VARCHAR(100) NOT NULL,
    name_es             VARCHAR(100),                   -- nombre en español
    whr_rank_2025       SMALLINT,
    whr_score_2025      NUMERIC(5,3),
    primary_language    VARCHAR(50)  NOT NULL,
    secondary_language  VARCHAR(50),                    -- NULL si no aplica
    tier                tier_type    NOT NULL DEFAULT 'T2',
    reddit_blocked      BOOLEAN      NOT NULL DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Datos iniciales — 19 países
INSERT INTO countries
    (iso2, iso3, name, name_es, whr_rank_2025, whr_score_2025,
     primary_language, secondary_language, tier, reddit_blocked, notes)
VALUES
    -- Tier 1
    ('US','USA','United States',    'Estados Unidos', 23, 6.816, 'en',    NULL,    'T1', FALSE, NULL),
    ('GB','GBR','United Kingdom',   'Reino Unido',    29, 6.694, 'en',    NULL,    'T1', FALSE, NULL),
    ('CA','CAN','Canada',           'Canadá',         25, 6.741, 'en',    NULL,    'T1', FALSE, NULL),
    ('AU','AUS','Australia',        'Australia',      15, 6.916, 'en',    NULL,    'T1', FALSE, NULL),
    ('BR','BRA','Brazil',           'Brasil',         32, 6.634, 'pt',    NULL,    'T1', FALSE, NULL),
    ('IN','IND','India',            'India',         116, 4.536, 'en',    'hi',    'T1', FALSE, NULL),
    ('MX','MEX','Mexico',           'México',         12, 6.972, 'es',    NULL,    'T1', FALSE, NULL),
    ('AR','ARG','Argentina',        'Argentina',      44, 6.430, 'es',    NULL,    'T1', FALSE, NULL),
    ('DE','DEU','Germany',          'Alemania',       17, 6.882, 'de',    NULL,    'T1', FALSE, NULL),
    ('FR','FRA','France',           'Francia',        35, 6.586, 'fr',    NULL,    'T1', FALSE, NULL),
    -- Tier 2
    ('PH','PHL','Philippines',      'Filipinas',      56, 6.206, 'en',    'tl',    'T2', FALSE, 'Code-switching Taglish frecuente. Filtrar confianza >= 0.7'),
    ('JP','JPN','Japan',            'Japón',          61, 6.130, 'ja',    'en',    'T2', FALSE, 'r/japan mayormente en inglés'),
    ('ZA','ZAF','South Africa',     'Sudáfrica',     101, 5.009, 'en',    NULL,    'T2', FALSE, NULL),
    ('IT','ITA','Italy',            'Italia',         38, 6.574, 'it',    NULL,    'T2', FALSE, NULL),
    ('PL','POL','Poland',           'Polonia',        24, 6.768, 'pl',    NULL,    'T2', FALSE, NULL),
    ('TR','TUR','Turkey',           'Turquía',        94, 5.300, 'tr',    NULL,    'T2', FALSE, NULL),
    ('KR','KOR','South Korea',      'Corea del Sur',  67, 6.040, 'ko',    'en',    'T2', FALSE, 'r/korea en inglés (diáspora). YouTube es fuente primaria'),
    ('ID','IDN','Indonesia',        'Indonesia',      87, 5.617, 'id',    NULL,    'T2', TRUE,  'Reddit bloqueado. Fuente primaria: YouTube + Facebook'),
    ('VN','VNM','Vietnam',          'Vietnam',        45, 6.428, 'vi',    NULL,    'T2', FALSE, 'Usar PhoBERT o XLM-R fine-tuned para vietnamita');

-- ============================================================
-- 2. PLATFORMS
-- ============================================================

CREATE TABLE platforms (
    id              SERIAL PRIMARY KEY,
    slug            VARCHAR(30)        NOT NULL UNIQUE,
    name            VARCHAR(100)       NOT NULL,
    api_type        platform_api_type  NOT NULL,
    has_raw_text    BOOLEAN            NOT NULL DEFAULT TRUE,
    base_url        VARCHAR(200),
    access_notes    TEXT,
    created_at      TIMESTAMPTZ        NOT NULL DEFAULT NOW()
);

INSERT INTO platforms (slug, name, api_type, has_raw_text, base_url, access_notes)
VALUES
    ('reddit',      'Reddit (Pushshift)',          'dataset',  TRUE,  'https://academictorrents.com',         'Dumps por subreddit. Descargar selectivamente por país'),
    ('youtube',     'YouTube Data API v3',         'push',     TRUE,  'https://developers.google.com/youtube','Comentarios de videos trending. Sin restricción de volumen'),
    ('youtube_ds',  'YouTube Trending 2022-2025',  'dataset',  FALSE, 'https://databank.illinois.edu',        '104 países, 446K snapshots. Solo metadatos, sin texto de comentarios'),
    ('facebook',    'Facebook / Meta',             'scraping', TRUE,  'https://developers.facebook.com',      'Meta Content Library (acceso académico) o scraping ético de páginas públicas'),
    ('tsgi',        'TSGI MIT/Harvard',            'dataset',  FALSE, 'https://doi.org/10.7910/DVN/3IL00Q',  'Índice agregado por país/día. Sin texto crudo. CC BY 4.0'),
    ('x_archive',   'X/Twitter Archive (pre-2023)','dataset',  TRUE,  'https://huggingface.co',               'Datasets archivados con geolocalización. Cobertura irregular por país');

-- ============================================================
-- 3. WHR_SCORES
-- ============================================================

CREATE TABLE whr_scores (
    id                  SERIAL PRIMARY KEY,
    country_id          INTEGER     NOT NULL REFERENCES countries(id) ON DELETE CASCADE,
    year                SMALLINT    NOT NULL,
    score               NUMERIC(5,3),
    gdp                 NUMERIC(5,3),
    social_support      NUMERIC(5,3),
    healthy_life        NUMERIC(5,3),
    freedom             NUMERIC(5,3),
    generosity          NUMERIC(5,3),
    corruption          NUMERIC(5,3),
    dystopia_residual   NUMERIC(5,3),
    rank                SMALLINT,
    source_file         VARCHAR(100),
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (country_id, year)
);

CREATE INDEX idx_whr_country ON whr_scores(country_id);
CREATE INDEX idx_whr_year    ON whr_scores(year);

-- ============================================================
-- 4. POSTS
-- ============================================================

CREATE TABLE posts (
    id              BIGSERIAL PRIMARY KEY,
    country_id      INTEGER          NOT NULL REFERENCES countries(id),
    platform_id     INTEGER          NOT NULL REFERENCES platforms(id),
    body            TEXT             NOT NULL,
    lang_detected   VARCHAR(10),                   -- código ISO 639-1 detectado
    lang_expected   VARCHAR(10),                   -- idioma esperado según país
    posted_at       TIMESTAMPTZ,
    collected_at    TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    source_id       VARCHAR(200),                  -- ID original en la plataforma
    source_url      TEXT,
    source_type     post_source_type NOT NULL DEFAULT 'comment',
    subreddit       VARCHAR(100),                  -- solo para Reddit
    channel_id      VARCHAR(100),                  -- solo para YouTube
    sampled         BOOLEAN          NOT NULL DEFAULT TRUE,
    sample_month    CHAR(7),                       -- YYYY-MM para estratificación
    char_count      SMALLINT GENERATED ALWAYS AS (LENGTH(body)) STORED,
    CONSTRAINT chk_posted_range CHECK (
        posted_at IS NULL OR (
            posted_at >= '2022-01-01' AND posted_at < '2025-01-01'
        )
    )
);

-- Índices principales
CREATE INDEX idx_posts_country   ON posts(country_id);
CREATE INDEX idx_posts_platform  ON posts(platform_id);
CREATE INDEX idx_posts_posted    ON posts(posted_at);
CREATE INDEX idx_posts_month     ON posts(sample_month);
CREATE INDEX idx_posts_lang      ON posts(lang_detected);
CREATE INDEX idx_posts_sampled   ON posts(sampled) WHERE sampled = TRUE;

-- Búsqueda de texto (trigrams)
CREATE INDEX idx_posts_body_trgm ON posts USING GIN (body gin_trgm_ops);

-- ============================================================
-- 5. CLASSIFICATIONS
-- ============================================================

CREATE TABLE classifications (
    id              BIGSERIAL PRIMARY KEY,
    post_id         BIGINT           NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    subindicator    subindicator_type NOT NULL,
    sentiment       sentiment_type   NOT NULL,
    intensity       SMALLINT         NOT NULL CHECK (intensity BETWEEN 1 AND 3),
    confidence      NUMERIC(4,3)     NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    model_version   VARCHAR(50)      NOT NULL,     -- ej: claude-sonnet-4-20250514
    classified_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    accepted        BOOLEAN GENERATED ALWAYS AS (confidence >= 0.7) STORED,
    raw_response    JSONB,                         -- respuesta completa de la IA
    UNIQUE (post_id)                               -- una clasificación por post
);

CREATE INDEX idx_class_post       ON classifications(post_id);
CREATE INDEX idx_class_sub        ON classifications(subindicator);
CREATE INDEX idx_class_sentiment  ON classifications(sentiment);
CREATE INDEX idx_class_accepted   ON classifications(accepted) WHERE accepted = TRUE;
CREATE INDEX idx_class_confidence ON classifications(confidence);

-- ============================================================
-- 6. HF_CLASSIFICATIONS (Etapa 1 del pipeline híbrido)
-- ============================================================

CREATE TABLE hf_classifications (
    id            BIGSERIAL PRIMARY KEY,
    post_id       BIGINT          NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    hf_sentiment  sentiment_type  NOT NULL,
    hf_score      NUMERIC(5,4)    NOT NULL,  -- confianza del modelo HF (0-1)
    hf_model      VARCHAR(100)    NOT NULL,  -- ej: twitter-xlm-roberta-base-sentiment
    classified_at TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (post_id)
);

CREATE INDEX idx_hf_post      ON hf_classifications(post_id);
CREATE INDEX idx_hf_score     ON hf_classifications(hf_score);
CREATE INDEX idx_hf_sentiment ON hf_classifications(hf_sentiment);

COMMENT ON TABLE hf_classifications IS
    'Sentimiento HuggingFace (etapa 1). Sin subindicador WHR. '
    'Posts con hf_score < 0.55 se descartan antes de enviar a Claude.';

-- ============================================================
-- 7. TSGI_INDEX
-- ============================================================

CREATE TABLE tsgi_index (
    id              SERIAL PRIMARY KEY,
    country_id      INTEGER      NOT NULL REFERENCES countries(id),
    index_date      DATE         NOT NULL,
    sentiment_score NUMERIC(6,4),                 -- índice 0-1
    tweet_count     INTEGER,
    source          VARCHAR(50)  NOT NULL DEFAULT 'TSGI MIT/Harvard',
    loaded_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (country_id, index_date),
    CONSTRAINT chk_tsgi_range CHECK (
        index_date >= '2022-01-01' AND index_date < '2025-01-01'
    )
);

CREATE INDEX idx_tsgi_country ON tsgi_index(country_id);
CREATE INDEX idx_tsgi_date    ON tsgi_index(index_date);

-- ============================================================
-- 7. ANALYSIS_RESULTS
-- ============================================================

CREATE TABLE analysis_results (
    id                      SERIAL PRIMARY KEY,
    country_id              INTEGER          NOT NULL REFERENCES countries(id),
    platform_id             INTEGER          NOT NULL REFERENCES platforms(id),
    subindicator            subindicator_type NOT NULL,
    year                    SMALLINT         NOT NULL,
    month                   SMALLINT         CHECK (month BETWEEN 1 AND 12),
    -- Sentimiento
    n_positive              INTEGER          NOT NULL DEFAULT 0,
    n_negative              INTEGER          NOT NULL DEFAULT 0,
    n_neutral               INTEGER          NOT NULL DEFAULT 0,
    sample_size             INTEGER          NOT NULL DEFAULT 0,
    sentiment_net           NUMERIC(6,4),           -- (pos - neg) / total
    avg_intensity           NUMERIC(4,3),
    avg_confidence          NUMERIC(4,3),
    -- Comparación WHR
    whr_score               NUMERIC(5,3),           -- score del subindicador ese año
    whr_score_normalized    NUMERIC(6,4),           -- 0-1 normalizado
    sentiment_normalized    NUMERIC(6,4),           -- 0-1 normalizado
    gap                     NUMERIC(6,4),           -- sentimiento_norm - whr_norm
    -- Metadatos
    computed_at             TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    UNIQUE (country_id, platform_id, subindicator, year, month)
);

CREATE INDEX idx_results_country     ON analysis_results(country_id);
CREATE INDEX idx_results_platform    ON analysis_results(platform_id);
CREATE INDEX idx_results_sub         ON analysis_results(subindicator);
CREATE INDEX idx_results_year_month  ON analysis_results(year, month);
CREATE INDEX idx_results_gap         ON analysis_results(ABS(gap) DESC NULLS LAST);

-- ============================================================
-- VISTAS ÚTILES
-- ============================================================

-- Vista principal: posts aceptados con su clasificación
CREATE VIEW v_classified_posts AS
SELECT
    p.id            AS post_id,
    c.iso2          AS country,
    c.name_es       AS country_name,
    pl.slug         AS platform,
    p.body,
    p.lang_detected,
    p.posted_at,
    p.sample_month,
    cl.subindicator,
    cl.sentiment,
    cl.intensity,
    cl.confidence
FROM posts p
JOIN countries      c  ON c.id  = p.country_id
JOIN platforms      pl ON pl.id = p.platform_id
JOIN classifications cl ON cl.post_id = p.id
WHERE cl.accepted = TRUE;

-- Vista de brecha por país y subindicador (promedio 2022-2024)
CREATE VIEW v_gap_summary AS
SELECT
    c.iso2,
    c.name_es,
    c.whr_rank_2025,
    ar.subindicator,
    pl.slug                     AS platform,
    ROUND(AVG(ar.sentiment_net)::numeric, 4)          AS avg_sentiment_net,
    ROUND(AVG(ar.whr_score_normalized)::numeric, 4)   AS avg_whr_normalized,
    ROUND(AVG(ar.gap)::numeric, 4)                    AS avg_gap,
    SUM(ar.sample_size)                               AS total_posts
FROM analysis_results ar
JOIN countries c  ON c.id  = ar.country_id
JOIN platforms pl ON pl.id = ar.platform_id
GROUP BY c.iso2, c.name_es, c.whr_rank_2025, ar.subindicator, pl.slug
ORDER BY ABS(AVG(ar.gap)) DESC;

-- Vista de cobertura por país y plataforma
CREATE VIEW v_coverage AS
SELECT
    c.iso2,
    c.name_es,
    pl.slug         AS platform,
    COUNT(p.id)     AS total_posts,
    COUNT(cl.id)    AS classified,
    COUNT(cl.id) FILTER (WHERE cl.accepted) AS accepted,
    ROUND(COUNT(cl.id) FILTER (WHERE cl.accepted)::numeric /
          NULLIF(COUNT(p.id),0) * 100, 1) AS acceptance_rate_pct,
    MIN(p.posted_at)::date AS earliest,
    MAX(p.posted_at)::date AS latest
FROM posts p
JOIN countries      c  ON c.id  = p.country_id
JOIN platforms      pl ON pl.id = p.platform_id
LEFT JOIN classifications cl ON cl.post_id = p.id
GROUP BY c.iso2, c.name_es, pl.slug
ORDER BY c.iso2, pl.slug;

-- ============================================================
-- COMENTARIOS DE TABLAS
-- ============================================================

COMMENT ON TABLE countries         IS 'Países del estudio — 19 países, selección intencional por tractabilidad';
COMMENT ON TABLE platforms         IS 'Plataformas de redes sociales y datasets utilizados';
COMMENT ON TABLE whr_scores        IS 'Scores WHR por país y año, incluyendo los 6 subindicadores';
COMMENT ON TABLE posts             IS 'Texto crudo recolectado. Ventana: 2022-01-01 a 2024-12-31';
COMMENT ON TABLE classifications   IS 'Clasificación IA de cada post. accepted=TRUE si confianza >= 0.7';
COMMENT ON TABLE tsgi_index        IS 'Índice TSGI MIT/Harvard: sentimiento agregado por país/día. Cobertura disponible: hasta 2023 (sin datos 2024). Limitación documentada.';
COMMENT ON TABLE analysis_results  IS 'Resultados agregados: sentimiento neto vs score WHR normalizado';

COMMENT ON COLUMN classifications.accepted       IS 'Computed column: confidence >= 0.7';
COMMENT ON COLUMN posts.char_count               IS 'Computed column: LENGTH(body)';
COMMENT ON COLUMN analysis_results.gap           IS 'sentiment_normalized - whr_score_normalized. Positivo = más feliz en redes que en WHR';
