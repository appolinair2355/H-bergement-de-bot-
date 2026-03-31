"""
db.py — Couche base de données du Bot Manager
Tables : user_profiles, projects (multi-bots), bot_settings, activity_logs
"""
import json
import time
import threading
import psycopg2
from psycopg2 import pool as _pg_pool
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta
import logging

import config

logger = logging.getLogger(__name__)
DATABASE_URL = config.DATABASE_URL

# ── Cache mémoire (évite les requêtes DB répétées dans un même flux) ──────────
# Chaque entrée : {"data": ..., "ts": float(time.time())}
_CACHE_TTL_PROFILE = 30   # secondes — profil utilisateur
_CACHE_TTL_BOTS    = 15   # secondes — liste des bots (change plus souvent)

_profile_cache: dict[int, dict] = {}
_bots_cache:    dict[int, dict] = {}
_cache_lock = threading.Lock()


def _cache_get(store: dict, tid: int, ttl: float):
    with _cache_lock:
        entry = store.get(tid)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["data"]
    return None          # cache miss ou expiré


def _cache_set(store: dict, tid: int, data):
    with _cache_lock:
        store[tid] = {"data": data, "ts": time.time()}


def _cache_del(tid: int):
    """Invalide profil + bots pour cet utilisateur (après toute écriture)."""
    with _cache_lock:
        _profile_cache.pop(tid, None)
        _bots_cache.pop(tid, None)

# ── Pool de connexions (évite d'ouvrir/fermer TCP à chaque requête) ───────────
_pool: _pg_pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> _pg_pool.ThreadedConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None or _pool.closed:
            _pool = _pg_pool.ThreadedConnectionPool(
                minconn=2, maxconn=15, dsn=DATABASE_URL
            )
    return _pool


class _PooledConn:
    """Wrapper qui réinjecte la connexion dans le pool à l'appel de .close()."""
    __slots__ = ("_raw", "_pool")

    def __init__(self, raw, pool):
        object.__setattr__(self, "_raw",  raw)
        object.__setattr__(self, "_pool", pool)

    def cursor(self, *a, **kw):
        kw.setdefault("cursor_factory", RealDictCursor)
        return object.__getattribute__(self, "_raw").cursor(*a, **kw)

    def commit(self):
        object.__getattribute__(self, "_raw").commit()

    def rollback(self):
        object.__getattribute__(self, "_raw").rollback()

    def close(self):
        raw  = object.__getattribute__(self, "_raw")
        pool = object.__getattribute__(self, "_pool")
        pool.putconn(raw)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_raw"), name)


def get_connection() -> _PooledConn:
    if not DATABASE_URL:
        raise RuntimeError(
            "RENDER_DATABASE_URL n'est pas défini. "
            "Ajoutez votre URL PostgreSQL Render dans les secrets sous le nom RENDER_DATABASE_URL."
        )
    try:
        pool = _get_pool()
        raw  = pool.getconn()
        raw.autocommit = False
        return _PooledConn(raw, pool)
    except Exception:
        # Fallback sans pool si le pool est saturé
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)  # type: ignore


# ══════════════════════════════════════════════════════════════════════════════
# INIT & MIGRATIONS
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    conn = get_connection()
    cur  = conn.cursor()

    # ── Table profils utilisateurs ───────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            telegram_id         BIGINT PRIMARY KEY,
            nom                 TEXT NOT NULL DEFAULT '',
            prenom              TEXT NOT NULL DEFAULT '',
            profile_env_vars    JSONB NOT NULL DEFAULT '{}',
            subscription_end    TIMESTAMP DEFAULT NULL,
            pro_subscription_end TIMESTAMP DEFAULT NULL,
            trial_used          BOOLEAN NOT NULL DEFAULT FALSE,
            date_registration   TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    for col, defn in [
        ("profile_env_vars",     "JSONB NOT NULL DEFAULT '{}'"),
        ("pro_subscription_end", "TIMESTAMP DEFAULT NULL"),
        ("trial_used",           "BOOLEAN NOT NULL DEFAULT FALSE"),
    ]:
        cur.execute(f"ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS {col} {defn}")

    # ── Table bots (multi-bots par utilisateur) ──────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id             SERIAL PRIMARY KEY,
            project_number INTEGER NOT NULL,
            telegram_id    BIGINT NOT NULL,
            project_name   TEXT NOT NULL DEFAULT 'Bot Principal',
            nom            TEXT NOT NULL DEFAULT '',
            prenom         TEXT NOT NULL DEFAULT '',
            api_token      TEXT NOT NULL,
            main_py        TEXT NOT NULL,
            extra_files    JSONB NOT NULL DEFAULT '{}',
            env_vars       JSONB NOT NULL DEFAULT '{}',
            date_creation  TIMESTAMP NOT NULL DEFAULT NOW(),
            is_running     BOOLEAN NOT NULL DEFAULT FALSE,
            pid            INTEGER DEFAULT NULL
        )
    """)
    # Migrations colonnes
    for col, defn in [
        ("project_name", "TEXT NOT NULL DEFAULT 'Bot Principal'"),
        ("extra_files",  "JSONB NOT NULL DEFAULT '{}'"),
        ("env_vars",     "JSONB NOT NULL DEFAULT '{}'"),
        ("assigned_port", "INTEGER DEFAULT NULL"),
    ]:
        cur.execute(f"ALTER TABLE projects ADD COLUMN IF NOT EXISTS {col} {defn}")

    # Suppression de l'ancienne contrainte UNIQUE(telegram_id) si elle existe
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name='projects' AND constraint_type='UNIQUE'
          AND constraint_name LIKE '%telegram_id%'
          AND constraint_name NOT LIKE '%project%'
    """)
    old = cur.fetchone()
    if old:
        cur.execute(f"ALTER TABLE projects DROP CONSTRAINT {old['constraint_name']}")

    # Nouvelle contrainte UNIQUE(telegram_id, project_name)
    cur.execute("""
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name='projects' AND constraint_name='projects_tid_pname_key'
    """)
    if not cur.fetchone():
        try:
            cur.execute("""
                ALTER TABLE projects
                ADD CONSTRAINT projects_tid_pname_key UNIQUE (telegram_id, project_name)
            """)
        except Exception as e:
            logger.warning(f"Constraint already exists: {e}")
            conn.rollback()
            conn = get_connection()
            cur  = conn.cursor()

    # Migration : copier les données subscription_end de projects vers user_profiles
    # (seulement si la colonne subscription_end existe encore dans projects)
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name='projects' AND column_name='subscription_end'
    """)
    if cur.fetchone():
        cur.execute("""
            INSERT INTO user_profiles (telegram_id, nom, prenom, subscription_end, trial_used)
            SELECT DISTINCT ON (telegram_id) telegram_id, nom, prenom, subscription_end, TRUE
            FROM projects
            WHERE nom IS NOT NULL AND nom != ''
            ON CONFLICT (telegram_id) DO UPDATE
                SET nom    = EXCLUDED.nom,
                    prenom = EXCLUDED.prenom
        """)
    else:
        # Juste migrer nom/prenom si disponibles
        cur.execute("""
            INSERT INTO user_profiles (telegram_id, nom, prenom, trial_used)
            SELECT DISTINCT ON (telegram_id) telegram_id, nom, prenom, TRUE
            FROM projects
            WHERE nom IS NOT NULL AND nom != ''
            ON CONFLICT (telegram_id) DO UPDATE
                SET nom    = CASE WHEN EXCLUDED.nom != '' THEN EXCLUDED.nom
                                  ELSE user_profiles.nom END,
                    prenom = CASE WHEN EXCLUDED.prenom != '' THEN EXCLUDED.prenom
                                  ELSE user_profiles.prenom END
        """)

    # Nettoyage : retirer la colonne subscription_end de projects si elle existe encore
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name='projects' AND column_name='subscription_end'
    """)
    if cur.fetchone():
        cur.execute("ALTER TABLE projects DROP COLUMN IF EXISTS subscription_end")

    # ── Table paramètres admin ───────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    for key, default in [
        ("payment_info",       config.DEFAULT_PAYMENT_INFO),
        ("price_7_days",       str(config.DEFAULT_PRICE_7_DAYS)),
        ("pro_price_per_week", str(config.DEFAULT_PRO_PRICE_PER_WEEK)),
    ]:
        cur.execute("""
            INSERT INTO bot_settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO NOTHING
        """, (key, default))

    # ── Table journal d'activité ─────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id          BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            action      TEXT   NOT NULL,
            details     TEXT   NOT NULL DEFAULT '',
            ts          TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS activity_logs_tid_idx ON activity_logs (telegram_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS activity_logs_ts_idx  ON activity_logs (ts DESC)")

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database initialized (multi-bot schema)")


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS (paramètres admin)
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT value FROM bot_settings WHERE key = %s", (key,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO bot_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (key, value))
    conn.commit()
    cur.close()
    conn.close()

def get_durations() -> list[dict]:
    """Retourne les durées/prix calculés depuis la base."""
    price_7 = int(get_setting("price_7_days", str(config.DEFAULT_PRICE_7_DAYS)))
    rate    = price_7 / (7 * 24)
    return [
        {"label": "1 heure",   "hours": 1,   "price": round(rate * 1)},
        {"label": "24 heures", "hours": 24,  "price": round(rate * 24)},
        {"label": "72 heures", "hours": 72,  "price": round(rate * 72)},
        {"label": "7 jours",   "hours": 168, "price": price_7},
    ]

def get_pro_price() -> int:
    return int(get_setting("pro_price_per_week", str(config.DEFAULT_PRO_PRICE_PER_WEEK)))


# ══════════════════════════════════════════════════════════════════════════════
# PROFILS UTILISATEURS
# ══════════════════════════════════════════════════════════════════════════════

def get_user_profile(telegram_id: int):
    cached = _cache_get(_profile_cache, telegram_id, _CACHE_TTL_PROFILE)
    if cached is not None:
        return cached
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM user_profiles WHERE telegram_id = %s", (telegram_id,))
    row  = cur.fetchone()
    cur.close()
    conn.close()
    if row is not None:
        _cache_set(_profile_cache, telegram_id, row)
    return row

def upsert_user_profile(telegram_id: int, nom: str = "", prenom: str = "",
                         profile_env_vars: dict = None):
    conn = get_connection()
    cur  = conn.cursor()
    env_json = json.dumps(profile_env_vars or {})
    cur.execute("""
        INSERT INTO user_profiles (telegram_id, nom, prenom, profile_env_vars)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (telegram_id) DO UPDATE SET
            nom              = CASE WHEN EXCLUDED.nom != '' THEN EXCLUDED.nom
                                    ELSE user_profiles.nom END,
            prenom           = CASE WHEN EXCLUDED.prenom != '' THEN EXCLUDED.prenom
                                    ELSE user_profiles.prenom END,
            profile_env_vars = CASE WHEN EXCLUDED.profile_env_vars != '{}'::jsonb
                                    THEN EXCLUDED.profile_env_vars
                                    ELSE user_profiles.profile_env_vars END
        RETURNING *
    """, (telegram_id, nom, prenom, env_json))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)
    return row

def give_free_trial(telegram_id: int):
    """Crée le profil avec 2h d'essai gratuit. Ne fait rien si déjà utilisé."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO user_profiles (telegram_id, trial_used, subscription_end)
        VALUES (%s, TRUE, NOW() + INTERVAL '%s hours')
        ON CONFLICT (telegram_id) DO NOTHING
    """, (telegram_id, config.FREE_TRIAL_HOURS))
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)

def is_subscription_active(telegram_id: int) -> bool:
    profile = get_user_profile(telegram_id)
    if not profile:
        return False
    sub_end = profile.get("subscription_end")
    if not sub_end:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(sub_end, "tzinfo") and sub_end.tzinfo is not None:
        sub_end = sub_end.replace(tzinfo=None)
    return sub_end > now

def is_pro_active(telegram_id: int) -> bool:
    profile = get_user_profile(telegram_id)
    if not profile:
        return False
    pro_end = profile.get("pro_subscription_end")
    if not pro_end:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(pro_end, "tzinfo") and pro_end.tzinfo is not None:
        pro_end = pro_end.replace(tzinfo=None)
    return pro_end > now

def set_subscription(telegram_id: int, hours: int):
    """Active ou prolonge l'abonnement de N heures (depuis user_profiles)."""
    conn = get_connection()
    cur  = conn.cursor()
    # S'assurer que le profil existe
    cur.execute("""
        INSERT INTO user_profiles (telegram_id) VALUES (%s)
        ON CONFLICT (telegram_id) DO NOTHING
    """, (telegram_id,))
    cur.execute("""
        UPDATE user_profiles
        SET subscription_end = GREATEST(NOW(), COALESCE(subscription_end, NOW()))
                               + INTERVAL '1 hour' * %s
        WHERE telegram_id = %s
        RETURNING subscription_end
    """, (hours, telegram_id))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)
    return row["subscription_end"] if row else None

def set_subscription_days(telegram_id: int, days: int):
    return set_subscription(telegram_id, days * 24)

def set_pro_subscription(telegram_id: int, weeks: int):
    """Active ou prolonge l'abonnement Pro de N semaines."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO user_profiles (telegram_id) VALUES (%s)
        ON CONFLICT (telegram_id) DO NOTHING
    """, (telegram_id,))
    cur.execute("""
        UPDATE user_profiles
        SET pro_subscription_end = GREATEST(NOW(), COALESCE(pro_subscription_end, NOW()))
                                   + INTERVAL '1 week' * %s
        WHERE telegram_id = %s
        RETURNING pro_subscription_end
    """, (weeks, telegram_id))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)
    return row["pro_subscription_end"] if row else None

def revoke_subscription(telegram_id: int):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE user_profiles
        SET subscription_end = NOW() - INTERVAL '1 second',
            pro_subscription_end = NOW() - INTERVAL '1 second'
        WHERE telegram_id = %s
    """, (telegram_id,))
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)

def get_all_profiles():
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM user_profiles ORDER BY date_registration ASC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def delete_user(telegram_id: int):
    """Supprime le profil ET tous les bots d'un utilisateur."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM projects WHERE telegram_id = %s", (telegram_id,))
    cur.execute("DELETE FROM user_profiles WHERE telegram_id = %s", (telegram_id,))
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)


# ══════════════════════════════════════════════════════════════════════════════
# BOTS (projets multi-bots)
# ══════════════════════════════════════════════════════════════════════════════

def _next_project_number() -> int:
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(project_number), 0) + 1 AS n FROM projects")
    n = cur.fetchone()["n"]
    cur.close()
    conn.close()
    return n

def save_bot(telegram_id: int, project_name: str, api_token: str,
             main_py: str, extra_files: dict = None, env_vars: dict = None,
             nom: str = "", prenom: str = ""):
    """Sauvegarde un bot (upsert par telegram_id + project_name)."""
    conn       = get_connection()
    cur        = conn.cursor()
    proj_num   = _next_project_number()
    extra_json = json.dumps(extra_files or {})
    env_json   = json.dumps(env_vars   or {})
    cur.execute("""
        INSERT INTO projects
            (project_number, telegram_id, project_name, nom, prenom,
             api_token, main_py, extra_files, env_vars)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (telegram_id, project_name) DO UPDATE SET
            api_token    = EXCLUDED.api_token,
            main_py      = EXCLUDED.main_py,
            extra_files  = EXCLUDED.extra_files,
            env_vars     = EXCLUDED.env_vars,
            nom          = EXCLUDED.nom,
            prenom       = EXCLUDED.prenom,
            date_creation = NOW(),
            is_running   = FALSE,
            pid          = NULL
        RETURNING project_number, date_creation
    """, (proj_num, telegram_id, project_name, nom, prenom,
          api_token, main_py, extra_json, env_json))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)
    return row

def get_bot(telegram_id: int, project_name: str):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE telegram_id=%s AND project_name=%s",
                (telegram_id, project_name))
    row  = cur.fetchone()
    cur.close()
    conn.close()
    return row

def get_user_bots(telegram_id: int) -> list:
    cached = _cache_get(_bots_cache, telegram_id, _CACHE_TTL_BOTS)
    if cached is not None:
        return cached
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM projects WHERE telegram_id=%s ORDER BY project_number ASC",
                (telegram_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    _cache_set(_bots_cache, telegram_id, rows)
    return rows

def count_user_bots(telegram_id: int) -> int:
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM projects WHERE telegram_id=%s", (telegram_id,))
    n = cur.fetchone()["cnt"]
    cur.close()
    conn.close()
    return n

def set_bot_running(telegram_id: int, project_name: str, is_running: bool, pid: int = None):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE projects SET is_running=%s, pid=%s
        WHERE telegram_id=%s AND project_name=%s
    """, (is_running, pid, telegram_id, project_name))
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)   # le statut bot a changé

def set_all_bots_stopped(telegram_id: int):
    """Marque tous les bots d'un utilisateur comme arrêtés."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("UPDATE projects SET is_running=FALSE, pid=NULL WHERE telegram_id=%s",
                (telegram_id,))
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)


def get_bot_assigned_port(telegram_id: int, project_name: str) -> int | None:
    """Retourne le port fixe assigné à ce bot (None si non encore assigné)."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT assigned_port FROM projects WHERE telegram_id=%s AND project_name=%s",
                (telegram_id, project_name))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["assigned_port"] if row else None


def set_bot_assigned_port(telegram_id: int, project_name: str, port: int):
    """Enregistre le port fixe assigné à ce bot."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE projects SET assigned_port=%s
        WHERE telegram_id=%s AND project_name=%s
    """, (port, telegram_id, project_name))
    conn.commit()
    cur.close()
    conn.close()


def get_all_assigned_ports() -> set:
    """Retourne l'ensemble de tous les ports déjà assignés (pour éviter les doublons)."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT assigned_port FROM projects WHERE assigned_port IS NOT NULL")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r["assigned_port"] for r in rows}

def get_all_bots() -> list:
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM projects ORDER BY telegram_id, project_number ASC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_expired_running_bots(admin_ids: list = None) -> list:
    """Retourne tous les bots actifs dont l'abonnement a expiré (exclut les admins)."""
    conn = get_connection()
    cur  = conn.cursor()
    # On exclut les admins — leurs bots tournent indéfiniment
    if admin_ids:
        placeholders = ",".join(["%s"] * len(admin_ids))
        cur.execute(f"""
            SELECT p.telegram_id, p.project_name, p.pid, p.api_token
            FROM projects p
            JOIN user_profiles u ON u.telegram_id = p.telegram_id
            WHERE p.is_running = TRUE
              AND p.telegram_id NOT IN ({placeholders})
              AND (u.subscription_end IS NULL OR u.subscription_end < NOW())
        """, admin_ids)
    else:
        cur.execute("""
            SELECT p.telegram_id, p.project_name, p.pid, p.api_token
            FROM projects p
            JOIN user_profiles u ON u.telegram_id = p.telegram_id
            WHERE p.is_running = TRUE
              AND (u.subscription_end IS NULL OR u.subscription_end < NOW())
        """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# ── Compat. runner.py (fonctions legacy) ─────────────────────────────────────
def get_project(telegram_id: int):
    """Compat. : retourne le premier bot de l'utilisateur."""
    bots = get_user_bots(telegram_id)
    return bots[0] if bots else None

def set_running(telegram_id: int, is_running: bool, pid: int = None,
                project_name: str = None):
    if project_name:
        set_bot_running(telegram_id, project_name, is_running, pid)
    else:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("UPDATE projects SET is_running=%s, pid=%s WHERE telegram_id=%s",
                    (is_running, pid, telegram_id))
        conn.commit()
        cur.close()
        conn.close()

def delete_bot(telegram_id: int, project_name: str):
    """Supprime un seul bot d'un utilisateur."""
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute(
        "DELETE FROM projects WHERE telegram_id=%s AND project_name=%s",
        (telegram_id, project_name)
    )
    conn.commit()
    cur.close()
    conn.close()
    _cache_del(telegram_id)

def delete_project(telegram_id: int):
    """Compat. : supprime tous les bots ET le profil."""
    delete_user(telegram_id)

def get_all_projects():
    return get_all_bots()


# ══════════════════════════════════════════════════════════════════════════════
# JOURNAL D'ACTIVITÉ
# ══════════════════════════════════════════════════════════════════════════════

def log_activity(telegram_id: int, action: str, details: str = "") -> None:
    """Enregistre une action utilisateur (fire-and-forget, erreurs silencieuses)."""
    def _write():
        try:
            conn = get_connection()
            cur  = conn.cursor()
            cur.execute(
                "INSERT INTO activity_logs (telegram_id, action, details) VALUES (%s, %s, %s)",
                (telegram_id, action, details[:2000])
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as exc:
            logger.warning(f"log_activity error: {exc}")
    threading.Thread(target=_write, daemon=True, name="log_activity").start()


def get_activity_logs(telegram_id: int = None, limit: int = 200) -> list:
    """Retourne les dernières entrées du journal (tous users si telegram_id=None)."""
    conn = get_connection()
    cur  = conn.cursor()
    if telegram_id:
        cur.execute(
            "SELECT * FROM activity_logs WHERE telegram_id=%s ORDER BY ts DESC LIMIT %s",
            (telegram_id, limit)
        )
    else:
        cur.execute(
            "SELECT * FROM activity_logs ORDER BY ts DESC LIMIT %s",
            (limit,)
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
