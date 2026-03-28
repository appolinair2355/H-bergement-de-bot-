"""
config.py — Configuration centralisée du Bot Manager
"""
import os

# ── Base de données (Render.com uniquement) ──────────────────────────────────
DATABASE_URL: str = os.environ.get("RENDER_DATABASE_URL", "")

# ── Telegram Bot ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ── Administrateurs ──────────────────────────────────────────────────────────
_raw_admin_ids = os.environ.get("ADMIN_TELEGRAM_IDS", "1190237801")
ADMIN_TELEGRAM_IDS: list[int] = [
    int(x.strip()) for x in _raw_admin_ids.split(",") if x.strip().isdigit()
]

# ── Serveur web (dashboard admin) ────────────────────────────────────────────
PORT: int = int(os.environ.get("PORT", 10000))
DASHBOARD_SECRET: str = os.environ.get("DASHBOARD_SECRET", "botmanager_admin_2024")

# ── Essai gratuit ────────────────────────────────────────────────────────────
FREE_TRIAL_HOURS: int = 2

# ── Limites de bots par plan ─────────────────────────────────────────────────
MAX_BOTS_BASIC: int = 5    # Plan standard (abonnement normal)
MAX_BOTS_PRO: int   = 10   # Plan Pro

# ── Tarification ─────────────────────────────────────────────────────────────
# Ces valeurs sont des DÉFAUTS ; l'admin peut les modifier depuis le bot via ⚙️ Config.
# Les vraies valeurs actives sont lues depuis la table bot_settings en base.
DEFAULT_PRICE_7_DAYS:       int = 1000
DEFAULT_PRO_PRICE_PER_WEEK: int = 2000

# ── Fallback payment info (remplacé par bot_settings en base) ────────────────
DEFAULT_PAYMENT_INFO: str = (
    "💳 *Instructions de paiement*\n\n"
    "Contactez l'administrateur pour effectuer votre paiement."
)
