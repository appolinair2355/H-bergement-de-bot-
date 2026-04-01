"""
bot.py — Bot Manager principal
Fonctionnalités : accueil intelligent, essai gratuit 2h, multi-bots,
limites par plan, paiement avec screenshot, config admin, auto-restart.
"""
import asyncio
import io
import json
import logging
import os
import threading
import zipfile
import runner as runner_module

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes,
)
from datetime import datetime, timezone

import py_compile
import tempfile
import config
from db import (
    init_db,
    # Profils
    get_user_profile, upsert_user_profile, give_free_trial,
    is_subscription_active, is_pro_active,
    set_subscription, set_subscription_days, set_pro_subscription, revoke_subscription,
    get_all_profiles, delete_user,
    block_user, unblock_user, is_user_blocked,
    # Bots
    save_bot, get_bot, get_user_bots, count_user_bots,
    delete_bot, update_bot_code, get_all_bots,
    set_bot_running, set_all_bots_stopped,
    # Settings
    get_setting, set_setting, get_durations, get_pro_price,
    # Compat
    get_project,
    # Journal
    log_activity, get_activity_logs,
    # Stats
    get_deployment_stats,
)
from runner import start_user_bot, stop_user_bot
from analyzer import detect_local_dependencies

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# ── États ─────────────────────────────────────────────────────────────────────
(NOM, PRENOM, API_ID_STEP, API_HASH_STEP, ADMIN_ID_STEP,
 PROJECT_NAME_STEP, API_TOKEN_STEP, ZIP_FILE,
 ENV_VAR_NAME, ENV_VAR_VALUE,
 ADMIN_PAY_MENU, ADMIN_PAY_EDIT_INFO, ADMIN_PAY_EDIT_PRICE,
 DEPLOY_TYPE_STEP, MODIFY_ZIP_STEP) = range(15)

# ── Paiements en attente de screenshot {telegram_id: {hours, price, label}} ──
_pending_payments: dict[int, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_admin(tid: int) -> bool:
    return tid in config.ADMIN_TELEGRAM_IDS

def _bot_limit(tid: int) -> int:
    """Nombre max de bots pour cet utilisateur."""
    if is_admin(tid):
        return 9999
    if is_pro_active(tid):
        return config.MAX_BOTS_PRO
    return config.MAX_BOTS_BASIC

def _sub_remaining_str(sub_end) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(sub_end, "tzinfo") and sub_end.tzinfo is not None:
        sub_end = sub_end.replace(tzinfo=None)
    r = sub_end - now
    d, h = r.days, r.seconds // 3600
    m = (r.seconds % 3600) // 60
    return f"{d}j {h}h {m}min"

def _sub_expire_str(sub_end) -> str:
    if hasattr(sub_end, "tzinfo") and sub_end.tzinfo is not None:
        sub_end = sub_end.replace(tzinfo=None)
    return sub_end.strftime("%d/%m/%Y à %H:%M")

def _dur_keyboard(include_pro: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for d in get_durations():
        rows.append([InlineKeyboardButton(
            f"⏱ {d['label']} — {d['price']} F",
            callback_data=f"pay_dur:{d['hours']}")])
    if include_pro:
        pro_price = get_pro_price()
        rows.append([InlineKeyboardButton(
            f"⭐ Abonnement Pro (10 bots) 7 jours — {pro_price} F",
            callback_data="pay_pro")])
    rows.append([InlineKeyboardButton("❌ Annuler", callback_data="pay_cancel")])
    return InlineKeyboardMarkup(rows)

def _welcome_keyboard(tid: int) -> InlineKeyboardMarkup:
    if is_admin(tid):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistiques",            callback_data="admin_stats"),
             InlineKeyboardButton("👥 Utilisateurs",            callback_data="admin_users")],
            [InlineKeyboardButton("🚀 Héberger un projet",      callback_data="begin_setup")],
            [InlineKeyboardButton("⚙️ Paiements",               callback_data="admin_pay_config"),
             InlineKeyboardButton("📦 ZIP source",              callback_data="admin_source_zip")],
            [InlineKeyboardButton("📖 Mode d'emploi",           callback_data="guide")],
        ])
    profile = get_user_profile(tid)
    bots    = get_user_bots(tid) if profile else []
    rows    = []
    if bots:
        running = sum(1 for b in bots if b["is_running"])
        label   = f"📋 Mes projets ({running} actif{'s' if running != 1 else ''}/{len(bots)})"
        rows.append([InlineKeyboardButton(label, callback_data="my_bots_list")])
    rows += [
        [InlineKeyboardButton("🚀 Héberger un projet",    callback_data="begin_setup")],
        [InlineKeyboardButton("💳 Abonnement",            callback_data="payer_info"),
         InlineKeyboardButton("📖 Mode d'emploi",         callback_data="guide")],
    ]
    return InlineKeyboardMarkup(rows)

def _red_panel(profile: dict, tid: int = None) -> tuple[str, InlineKeyboardMarkup]:
    sub_end = profile.get("subscription_end") if profile else None
    if sub_end:
        msg = (f"🔴 *Abonnement expiré*\n\n"
               f"Expiré le : {_sub_expire_str(sub_end)}\n\n"
               "Renouvelez pour réactiver votre hébergement.")
    else:
        msg = ("🔴 *Aucun abonnement actif*\n\n"
               "Choisissez une durée pour activer l'hébergement de votre bot.")
    # Boutons de navigation complets
    rows = [[InlineKeyboardButton("💳 Payer mon abonnement", callback_data="payer_info")]]
    if tid:
        bots = get_user_bots(tid)
        if bots:
            rows.append([InlineKeyboardButton("📋 Mes projets", callback_data="my_bots_list")])
        rows.append([InlineKeyboardButton("🚀 Héberger un projet", callback_data="begin_setup")])
    rows.append([InlineKeyboardButton("📖 Mode d'emploi", callback_data="guide")])
    return msg, InlineKeyboardMarkup(rows)

def _blue_panel(tid: int) -> tuple[str, InlineKeyboardMarkup]:
    from db import get_bot_assigned_port
    profile = get_user_profile(tid) or {}
    sub_end = profile.get("subscription_end")
    pro_end = profile.get("pro_subscription_end")
    bots    = get_user_bots(tid)

    remaining = _sub_remaining_str(sub_end) if sub_end else "—"
    exp_str   = _sub_expire_str(sub_end)    if sub_end else "—"
    running_c = sum(1 for b in bots if b["is_running"])
    limit     = _bot_limit(tid)

    pro_line = ""
    if is_pro_active(tid) and pro_end:
        pro_line = f"\n├ ⭐ Pro : {_sub_remaining_str(pro_end)} restants"

    nb_bots = sum(1 for b in bots if b.get("project_type", "bot") == "bot")
    nb_web  = sum(1 for b in bots if b.get("project_type", "bot") == "website")

    # Tableau des projets
    project_lines = ""
    for b in bots:
        ico      = "🟢" if b["is_running"] else "🔴"
        ptype    = b.get("project_type", "bot")
        type_ico = "🌐" if ptype == "website" else "🤖"
        port     = get_bot_assigned_port(tid, b["project_name"]) if b["is_running"] else None
        port_str = f" · port {port}" if port else ""
        project_lines += f"\n│  {ico} {type_ico} <b>{b['project_name']}</b>{port_str}"

    type_summary = f"🤖 {nb_bots}  🌐 {nb_web}"
    msg = (
        "🔵 <b>Tableau de bord</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"├ ✅ Abonnement : <b>Actif</b>\n"
        f"├ ⏳ Restant : <b>{remaining}</b>\n"
        f"├ 📅 Expire : {exp_str}"
        f"{pro_line}\n"
        f"├ 📦 Projets : {len(bots)}/{limit}  ({type_summary})\n"
        f"│  {project_lines if project_lines else '  <i>Aucun projet</i>'}\n"
        f"└ 🟢 En ligne : {running_c}/{len(bots)}"
    )

    rows = []
    for b in bots:
        pname = b["project_name"]
        ptype = b.get("project_type", "bot")
        pname_short = pname[:18]
        if b["is_running"]:
            btn_toggle = InlineKeyboardButton(f"⛔ {pname_short}", callback_data=f"stop:{pname}")
        else:
            btn_toggle = InlineKeyboardButton(f"▶️ {pname_short}", callback_data=f"deploy:{pname}")
        btn_mod = InlineKeyboardButton("✏️", callback_data=f"modify:{pname}")
        btn_del = InlineKeyboardButton("🗑️", callback_data=f"del_ask:{pname}")
        rows.append([btn_toggle, btn_mod, btn_del])
        if ptype == "website" and b.get("website_url"):
            rows.append([InlineKeyboardButton(
                f"🌐 Ouvrir {pname_short}", url=b["website_url"])])

    rows.append([
        InlineKeyboardButton("➕ Nouveau projet",    callback_data="add_bot"),
        InlineKeyboardButton("💳 Renouveler",        callback_data="payer_info"),
    ])
    return msg, InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDE /start — Accueil intelligent
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    tid     = update.effective_user.id

    if is_user_blocked(tid):
        await update.message.reply_text(
            "🚫 *Votre compte a été bloqué.*\n\nContactez l'administrateur.",
            parse_mode="Markdown")
        return ConversationHandler.END

    profile = get_user_profile(tid)

    # Essai gratuit 2h : première visite
    if not profile:
        give_free_trial(tid)
        profile = get_user_profile(tid)

    # ── ADMIN : panneau d'administration complet ──────────────────────────────
    if is_admin(tid):
        from db import get_bot_assigned_port
        bots      = get_user_bots(tid)
        running_c = sum(1 for b in bots if b["is_running"])
        nb        = len(bots)

        # Description de chaque bot
        bot_lines = ""
        for b in bots:
            ico = "🟢" if b["is_running"] else "🔴"
            extra = b.get("extra_files") or {}
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    extra = {}
            n_files = len(extra)
            file_info = f" (+{n_files} fichier{'s' if n_files > 1 else ''})" if n_files else ""
            port = get_bot_assigned_port(tid, b["project_name"])
            port_info = f" <code>:{port}</code>" if (b["is_running"] and port) else ""
            bot_lines += f"\n│ {ico} <b>{b['project_name']}</b>{file_info}{port_info}"

        msg = (
            "🔑 <b>Panneau Administrateur</b>\n\n"
            f"├ 🤖 Mes bots ({nb}) :{bot_lines if bot_lines else ' <i>aucun</i>'}\n"
            f"└ 🟢 En ligne : <b>{running_c}/{nb}</b>"
        )

        # Boutons par bot : Démarrer/Arrêter + Télécharger
        rows = []
        for b in bots:
            pname = b["project_name"]
            btn_action = (
                InlineKeyboardButton(f"⛔ {pname}", callback_data=f"stop:{pname}")
                if b["is_running"] else
                InlineKeyboardButton(f"🚀 {pname}", callback_data=f"deploy:{pname}")
            )
            btn_dl = InlineKeyboardButton("📥", callback_data=f"adl:{tid}:{pname}")
            rows.append([btn_action, btn_dl])

        # Boutons de navigation admin
        rows += [
            [InlineKeyboardButton("👥 Mes Utilisateurs",       callback_data="admin_users")],
            [InlineKeyboardButton("➕ Héberger un Bot",         callback_data="begin_setup")],
            [InlineKeyboardButton("⚙️ Config paiements",       callback_data="admin_pay_config")],
            [InlineKeyboardButton("📖 Mode d'emploi",           callback_data="guide")],
        ]
        await update.message.reply_text(msg, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(rows))
        return ConversationHandler.END

    # ── Utilisateur connu avec nom enregistré ─────────────────────────────────
    if profile and (profile.get("nom") or "").strip():
        active = is_subscription_active(tid)
        if active:
            msg, kb2 = _blue_panel(tid)
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb2)
        else:
            msg, kb2 = _red_panel(profile, tid)
            prenom = (profile.get("prenom") or "").strip()
            nom    = (profile.get("nom") or "").strip()
            await update.message.reply_text(
                f"👋 Bon retour *{prenom} {nom}* !\n\n" + msg,
                parse_mode="Markdown", reply_markup=kb2)
        return ConversationHandler.END

    # ── Nouvel utilisateur ────────────────────────────────────────────────────
    trial_notice = (
        f"\n\n🎁 *Essai gratuit activé : {config.FREE_TRIAL_HOURS}h offerts !*\n"
        "_Utilisez ce temps pour configurer et tester votre bot._"
    )
    await update.message.reply_text(
        f"👋 *Bienvenue sur le Bot Manager !*\n\n"
        "Je vous aide à héberger vos bots Telegram en quelques étapes."
        f"{trial_notice}\n\n"
        "Choisissez une option :",
        parse_mode="Markdown",
        reply_markup=_welcome_keyboard(tid),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ENTRÉE DANS LE FLUX DE CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

async def begin_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    tid     = update.effective_user.id
    user    = update.effective_user
    profile = get_user_profile(tid)

    # Nettoyer les données de la session précédente pour éviter les conflits
    context.user_data.clear()

    # Enregistrer le nom Telegram automatiquement (sans le demander)
    tg_nom    = (user.last_name  or "").strip()
    tg_prenom = (user.first_name or "").strip()
    if not profile:
        give_free_trial(tid)
    upsert_user_profile(tid, nom=tg_nom, prenom=tg_prenom)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Héberger un Bot Telegram", callback_data="dep_type:bot")],
        [InlineKeyboardButton("🌐 Héberger un Site Web",     callback_data="dep_type:website")],
        [InlineKeyboardButton("🏠 Retour",                   callback_data="back_home")],
    ])
    await q.edit_message_text(
        "🚀 <b>Que souhaitez-vous héberger ?</b>\n\n"
        "• <b>🤖 Bot Telegram</b> — un bot qui tourne en arrière-plan\n"
        "• <b>🌐 Site Web</b> — une application web (Flask, FastAPI, etc.)\n\n"
        "<i>Choisissez le type de projet :</i>",
        parse_mode="HTML",
        reply_markup=kb)
    return DEPLOY_TYPE_STEP


async def deploy_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'utilisateur choisit Bot ou Site Web."""
    q    = update.callback_query
    await q.answer()
    tid  = update.effective_user.id
    dtype = q.data.split(":", 1)[1]
    context.user_data["deploy_type"] = dtype
    profile = get_user_profile(tid)

    if dtype == "website":
        await q.edit_message_text(
            "🌐 <b>Héberger un Site Web</b>\n\n"
            "📋 <b>Nom de votre projet ?</b>\n\n"
            "<i>Ex : Mon Blog, API Shop, Dashboard...\n(Max 30 caractères)</i>",
            parse_mode="HTML")
        return PROJECT_NAME_STEP

    # Bot Telegram — même logique qu'avant
    if is_admin(tid):
        context.user_data["is_returning"]     = True
        context.user_data["profile_env_vars"] = {}
        await q.edit_message_text(
            "🤖 <b>Admin — Héberger un bot</b>\n\n"
            "📋 <b>Nom de votre projet / bot ?</b>\n\n"
            "<i>Ex : Mon Scraper, Bot Shop, Assistant...\n(Max 30 caractères)</i>",
            parse_mode="HTML")
        return PROJECT_NAME_STEP

    has_credentials = bool(
        profile and
        (profile.get("profile_env_vars") or {}).get("API_ID")
    )
    if has_credentials:
        context.user_data["is_returning"]     = True
        context.user_data["profile_env_vars"] = dict(profile.get("profile_env_vars") or {})
        await q.edit_message_text(
            "📋 *Nom de votre projet / bot ?*\n\n"
            "_Ex : Mon Scraper, Bot Shop, Assistant..._\n_(Max 30 caractères)_",
            parse_mode="Markdown")
        return PROJECT_NAME_STEP
    else:
        context.user_data["is_returning"] = False
        await q.edit_message_text(
            "🔑 *Étape 1/3 — Entrez votre `API_ID`*\n\n"
            "_Nombre entier disponible sur [my.telegram.org](https://my.telegram.org)_",
            parse_mode="Markdown",
            disable_web_page_preview=True)
        return API_ID_STEP


# ══════════════════════════════════════════════════════════════════════════════
# FLUX COMPLET — Nouveaux utilisateurs (API_ID → API_HASH → ADMIN_ID)
# ══════════════════════════════════════════════════════════════════════════════

async def get_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()
    if not v.lstrip("-").isdigit():
        await update.message.reply_text("❌ `API_ID` doit être un entier. Réessayez :", parse_mode="Markdown")
        return API_ID_STEP
    context.user_data["api_id"] = v
    await update.message.reply_text(
        f"✅ `API_ID` : `{v}`\n\n"
        "🔑 *Étape 2/3 — Entrez votre `API_HASH`*\n"
        "_Disponible sur [my.telegram.org](https://my.telegram.org)._",
        parse_mode="Markdown", disable_web_page_preview=True)
    return API_HASH_STEP

async def get_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()
    if len(v) < 8:
        await update.message.reply_text("❌ `API_HASH` trop court. Réessayez :", parse_mode="Markdown")
        return API_HASH_STEP
    context.user_data["api_hash"] = v
    await update.message.reply_text(
        "✅ `API_HASH` enregistré.\n\n"
        "👤 *Étape 3/3 — Entrez votre `ADMIN_ID`*\n"
        "_Votre ID numérique Telegram (obtenez-le via @userinfobot)._",
        parse_mode="Markdown")
    return ADMIN_ID_STEP

async def get_admin_id_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()
    if not v.lstrip("-").isdigit():
        await update.message.reply_text("❌ `ADMIN_ID` doit être un entier. Réessayez :", parse_mode="Markdown")
        return ADMIN_ID_STEP
    context.user_data["admin_id"] = v
    # Sauvegarder les 3 credentials dans le profil
    profile_env = {
        "API_ID":   context.user_data["api_id"],
        "API_HASH": context.user_data["api_hash"],
        "ADMIN_ID": v,
    }
    upsert_user_profile(
        update.effective_user.id,
        profile_env_vars=profile_env,
    )
    context.user_data["profile_env_vars"] = profile_env
    await update.message.reply_text(
        "✅ *Credentials enregistrés !*\n\n"
        "Ces 3 valeurs (`API_ID`, `API_HASH`, `ADMIN_ID`) seront automatiquement\n"
        "réutilisées pour tous vos prochains bots.\n\n"
        "📋 *Nom de votre projet / bot ?*\n\n"
        "_Ex : Mon Assistant, Bot E-commerce, Scraper..._",
        parse_mode="Markdown")
    return PROJECT_NAME_STEP


# ══════════════════════════════════════════════════════════════════════════════
# FLUX COMMUN — Nom du projet, token, ZIP
# ══════════════════════════════════════════════════════════════════════════════

async def get_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tid  = update.effective_user.id
    name = update.message.text.strip()[:30]
    if not name:
        await update.message.reply_text("❌ Le nom ne peut pas être vide.")
        return PROJECT_NAME_STEP

    # Vérifier si ce nom de projet existe déjà → demander confirmation explicite
    existing = get_bot(tid, name)
    if existing:
        context.user_data["project_name_pending"] = name
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Oui, mettre à jour ce bot", callback_data=f"confirm_update:{name}")],
            [InlineKeyboardButton("❌ Non, choisir un autre nom",  callback_data="cancel_update")],
        ])
        await update.message.reply_text(
            f"⚠️ *Un bot nommé « {name} » existe déjà !*\n\n"
            "Mettre à jour écrasera :\n"
            f"• Le token actuel : `{existing['api_token'][:20]}...`\n"
            "• Le code source\n"
            "• Les variables d'environnement\n\n"
            "Voulez-vous vraiment *remplacer* ce bot ?",
            parse_mode="Markdown", reply_markup=kb)
        return PROJECT_NAME_STEP

    context.user_data["project_name"] = name
    # Sites web : pas de token Telegram
    if context.user_data.get("deploy_type") == "website":
        await update.message.reply_text(
            f"✅ Nom du projet : *{name}*\n\n"
            "📦 *Envoyez le fichier ZIP de votre site web*\n\n"
            "Le ZIP doit contenir :\n"
            "• `main.py` ou `app.py` _(point d'entrée Flask/FastAPI)_\n"
            "• Autres fichiers Python, HTML, CSS, JSON... (optionnel)\n"
            "• `.env` avec les variables d'environnement (optionnel)",
            parse_mode="Markdown")
        return ZIP_FILE
    await update.message.reply_text(
        f"✅ Nom du projet : *{name}*\n\n"
        "🔑 *Token API du bot ?*\n"
        "_Obtenez un token via @BotFather sur Telegram._",
        parse_mode="Markdown")
    return API_TOKEN_STEP

async def confirm_update_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'utilisateur confirme l'écrasement d'un bot existant."""
    q = update.callback_query
    await q.answer()
    name = context.user_data.get("project_name_pending") or q.data.split(":", 1)[1]
    context.user_data["project_name"] = name
    context.user_data.pop("project_name_pending", None)
    await q.edit_message_text(
        f"✅ Mise à jour du bot *{name}* confirmée.\n\n"
        "🔑 *Nouveau token API du bot ?*\n"
        "_Obtenez un token via @BotFather sur Telegram._",
        parse_mode="Markdown")
    return API_TOKEN_STEP

async def cancel_update_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'utilisateur annule et choisit un autre nom."""
    q = update.callback_query
    await q.answer()
    context.user_data.pop("project_name_pending", None)
    await q.edit_message_text(
        "📋 *Tapez un nouveau nom pour votre bot :*\n\n"
        "_Le nom doit être unique (ex : MonBot2, AssistantV2...)_",
        parse_mode="Markdown")
    return PROJECT_NAME_STEP

async def get_api_token_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    token = update.message.text.strip()
    if ":" not in token or len(token) < 30:
        await update.message.reply_text(
            "❌ Token invalide. Format : `123456:ABCdef...`\nRéessayez :",
            parse_mode="Markdown")
        return API_TOKEN_STEP
    context.user_data["api_token"] = token
    await update.message.reply_text(
        "✅ Token enregistré.\n\n"
        "📦 *Envoyez votre fichier ZIP*\n\n"
        "Le ZIP doit contenir :\n"
        "• `main.py` _(obligatoire)_\n"
        "• Autres fichiers Python (optionnel)\n"
        "• `.env` avec les variables d'environnement (optionnel)",
        parse_mode="Markdown")
    return ZIP_FILE

TEXT_EXTENSIONS = {
    ".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".html", ".htm", ".css", ".js", ".ts", ".md",
    ".xml", ".csv", ".env", ".sql", ".sh",
}

def _analyze_py_syntax(files: dict[str, str]) -> list[str]:
    """Vérifie la syntaxe de chaque fichier Python. Retourne la liste des erreurs."""
    errors = []
    for fname, code in files.items():
        if not fname.endswith(".py"):
            continue
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                             delete=False, encoding="utf-8") as f:
                f.write(code)
                tmp = f.name
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            msg = str(e)
            # Nettoyer le chemin tmp du message
            msg = msg.replace(tmp or "", fname) if tmp else msg
            errors.append(f"❌ `{fname}` : {msg.split(':', 2)[-1].strip()}")
        except Exception:
            pass
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    return errors


async def get_zip_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Envoyez un fichier `.zip`.", parse_mode="Markdown")
        return ZIP_FILE
    if not (doc.file_name or "").lower().endswith(".zip"):
        await update.message.reply_text(f"❌ `{doc.file_name}` n'est pas un ZIP.", parse_mode="Markdown")
        return ZIP_FILE

    wait = await update.message.reply_text("⬇️ *Téléchargement et analyse en cours...*", parse_mode="Markdown")
    zip_bytes = await (await doc.get_file()).download_as_bytearray()

    # ── Sauvegarder le ZIP brut pour téléchargement admin ───────────────────
    tid_save   = update.effective_user.id
    pname_save = context.user_data.get("project_name", "unknown")
    safe_save  = "".join(c if c.isalnum() or c == "_" else "_" for c in pname_save.lower())
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    zip_path   = os.path.join(upload_dir, f"{tid_save}_{safe_save}.zip")
    with open(zip_path, "wb") as _zf:
        _zf.write(zip_bytes)
    # ────────────────────────────────────────────────────────────────────────

    py_files:    dict[str, str] = {}
    extra_files: dict[str, str] = {}
    env_vars:    dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.endswith("/") or name.startswith("__MACOSX"):
                    continue
                basename = os.path.basename(name)
                if not basename:
                    continue
                ext = os.path.splitext(basename)[1].lower()
                if basename in (".env", "env"):
                    for line in zf.read(name).decode("utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        env_vars[k.strip()] = v.strip().strip('"').strip("'")
                elif basename.endswith(".py"):
                    try:
                        py_files[basename] = zf.read(name).decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                elif ext in TEXT_EXTENSIONS:
                    try:
                        extra_files[basename] = zf.read(name).decode("utf-8", errors="replace")
                    except Exception:
                        pass
    except zipfile.BadZipFile:
        await wait.edit_text("❌ ZIP invalide ou corrompu.")
        return ZIP_FILE

    # Détection automatique du fichier principal
    MAIN_PRIORITY = ["main.py", "bot.py", "app.py", "index.py", "run.py", "start.py"]
    main_filename = None
    for candidate in MAIN_PRIORITY:
        if candidate in py_files:
            main_filename = candidate
            break
    if main_filename is None and py_files:
        main_filename = sorted(py_files.keys())[0]

    if main_filename is None:
        await wait.edit_text(
            "❌ Aucun fichier Python `.py` trouvé dans le ZIP.\n\n"
            "Vérifiez que votre archive contient bien au moins un fichier `.py`.",
            parse_mode="Markdown")
        return ZIP_FILE

    main_code = py_files.pop(main_filename)
    # Fusionner les .py secondaires dans extra_files
    extra_files.update(py_files)

    # ── Analyse de syntaxe Python ────────────────────────────────────────────
    all_py = {"main.py": main_code}
    all_py.update({k: v for k, v in extra_files.items() if k.endswith(".py")})
    syntax_errors = _analyze_py_syntax(all_py)

    # ── Détection des dépendances pip ────────────────────────────────────────
    _, pip_deps = detect_local_dependencies(main_code)

    # ── Rapport d'analyse ────────────────────────────────────────────────────
    all_names = [main_filename] + sorted(extra_files.keys())
    env_note  = " + `.env` ✅" if env_vars else ""
    pkg_str   = "\n  ".join(f"✅ `{p}`" for p in pip_deps) if pip_deps else "Aucun détecté"

    analysis_text = (
        f"🔍 <b>Analyse du projet</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📁 <b>Fichiers ({len(all_names)})</b>{env_note} :\n"
        + "".join(f"  📄 <code>{f}</code>\n" for f in all_names)
        + f"\n📦 <b>Packages détectés :</b>\n  {pkg_str}\n"
    )

    if syntax_errors:
        analysis_text += "\n⚠️ <b>Erreurs de syntaxe détectées :</b>\n"
        analysis_text += "".join(f"  {e}\n" for e in syntax_errors[:5])
        analysis_text += "\n<i>Corrigez les erreurs avant de continuer.</i>"
    else:
        analysis_text += "\n✅ <b>Syntaxe Python : OK</b>"

    await wait.edit_text(analysis_text, parse_mode="HTML")

    if syntax_errors:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Continuer quand même", callback_data="envvar_continue")],
            [InlineKeyboardButton("🏠 Annuler",              callback_data="back_home")],
        ])
        await update.effective_message.reply_text(
            "⚠️ Des erreurs de syntaxe ont été trouvées.\n"
            "Vous pouvez corriger votre ZIP et le renvoyer, ou continuer quand même.",
            reply_markup=kb)
        # On stocke quand même pour permettre de continuer
    else:
        await update.effective_message.reply_text(
            "✅ <b>Analyse terminée — aucune erreur détectée.</b>\n\n"
            "Vous pouvez maintenant configurer les variables d'environnement.",
            parse_mode="HTML")

    # Fusionner avec les credentials du profil (API_ID, API_HASH, ADMIN_ID)
    profile_env = context.user_data.get("profile_env_vars") or {}
    for k, v in profile_env.items():
        env_vars.setdefault(k, v)

    # Stocker dans user_data pour la fenêtre env vars
    context.user_data["zip_main_code"]   = main_code
    context.user_data["zip_extra_files"] = extra_files
    context.user_data["zip_env_vars"]    = env_vars

    # Afficher la fenêtre Variables d'environnement
    return await _show_env_vars_panel(update, context)


# ══════════════════════════════════════════════════════════════════════════════
# FENÊTRE — Variables d'environnement
# ══════════════════════════════════════════════════════════════════════════════

async def _show_env_vars_panel(update, context) -> int:
    """Affiche la fenêtre de gestion des variables d'environnement."""
    env_vars = context.user_data.get("zip_env_vars") or {}

    header = "🔧 <b>Variables d'environnement</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    if env_vars:
        lines = "".join(
            f"  <code>{k}</code> = <code>{str(v)[:40]}{'…' if len(str(v)) > 40 else ''}</code>\n"
            for k, v in env_vars.items()
        )
        body = f"📋 <b>{len(env_vars)} variable(s) configurée(s) :</b>\n{lines}"
    else:
        body = "<i>Aucune variable configurée pour l'instant.</i>"

    footer = ("\n\n<i>Ces valeurs seront injectées dans votre bot "
              "au moment du démarrage.</i>")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ajouter une variable d'environnement",
                              callback_data="envvar_add")],
        [InlineKeyboardButton("✅ Terminer et héberger",
                              callback_data="envvar_done")],
        [InlineKeyboardButton("🏠 Annuler",
                              callback_data="back_home")],
    ])

    text = header + body + footer
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=kb)
    return ENV_VAR_NAME


async def env_var_continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'utilisateur veut continuer malgré les erreurs de syntaxe."""
    q = update.callback_query
    await q.answer()
    return await _show_env_vars_panel(update, context)


async def env_var_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'utilisateur tape sur ➕ Ajouter → demander le nom de la variable."""
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🔧 <b>Variables d'environnement</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✏️ <b>Nom de la variable :</b>\n\n"
        "<i>Exemples : <code>DATABASE_URL</code>, <code>SECRET_KEY</code>, <code>WEBHOOK_URL</code>...</i>\n\n"
        "Tapez le nom ci-dessous :",
        parse_mode="HTML")
    return ENV_VAR_NAME


async def get_env_var_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit le nom de la variable, demande la valeur."""
    name = update.message.text.strip().upper().replace(" ", "_")
    if not name:
        await update.message.reply_text("❌ Le nom ne peut pas être vide.")
        return ENV_VAR_NAME
    context.user_data["new_env_var_name"] = name
    await update.message.reply_text(
        f"🔧 <b>Variables d'environnement</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ Nom : <code>{name}</code>\n\n"
        f"🔑 <b>Valeur de <code>{name}</code> :</b>\n\n"
        "<i>Tapez la valeur ci-dessous :</i>",
        parse_mode="HTML")
    return ENV_VAR_VALUE


async def get_env_var_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit la valeur, ajoute la variable, réaffiche le panel."""
    value = update.message.text.strip()
    name  = context.user_data.pop("new_env_var_name", "VAR")
    env_vars = context.user_data.get("zip_env_vars") or {}
    env_vars[name] = value
    context.user_data["zip_env_vars"] = env_vars
    return await _show_env_vars_panel(update, context)


async def env_var_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'utilisateur tape sur ✅ Terminer → lancer le bot."""
    q = update.callback_query
    await q.answer()
    main_code   = context.user_data.get("zip_main_code", "")
    extra_files = context.user_data.get("zip_extra_files", {})
    env_vars    = context.user_data.get("zip_env_vars", {})
    return await _finalize_bot(update, context, main_code, extra_files, env_vars)


async def _finalize_bot(update, context, main_code, extra_files, env_vars):
    tid          = update.effective_user.id
    project_name = context.user_data.get("project_name", "")
    api_token    = context.user_data.get("api_token", "")
    deploy_type  = context.user_data.get("deploy_type", "bot")
    profile      = get_user_profile(tid) or {}
    nom          = profile.get("nom", "")
    prenom       = profile.get("prenom", "")
    msg          = update.effective_message

    if not project_name:
        await msg.reply_text(
            "❌ <b>Erreur de session.</b>\n\nTapez /start pour recommencer.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Retour", callback_data="back_home")
            ]]))
        return ConversationHandler.END

    if not main_code:
        await msg.reply_text(
            "❌ <b>Aucun code source.</b>\n\nTapez /start pour recommencer.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Retour", callback_data="back_home")
            ]]))
        return ConversationHandler.END

    # Pour les bots, token obligatoire
    if deploy_type == "bot" and not api_token:
        await msg.reply_text(
            "❌ <b>Token manquant.</b>\n\nTapez /start pour recommencer.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Retour", callback_data="back_home")
            ]]))
        return ConversationHandler.END

    # Vérification limite de projets (admins = illimité)
    if not is_admin(tid):
        limit    = _bot_limit(tid)
        nb_projs = count_user_bots(tid)
        existing = get_bot(tid, project_name)
        if not existing and nb_projs >= limit:
            if not is_pro_active(tid) and limit == config.MAX_BOTS_BASIC:
                pro_price = get_pro_price()
                kb = [[InlineKeyboardButton(
                    f"⭐ Passer Pro ({config.MAX_BOTS_PRO} projets) — {pro_price} F/sem",
                    callback_data="pay_pro")],
                    [InlineKeyboardButton("🏠 Retour", callback_data="back_home")]]
                await msg.reply_text(
                    f"⛔ <b>Limite atteinte : {config.MAX_BOTS_BASIC} projets</b>\n\n"
                    "Passez au plan <b>Pro</b> pour héberger plus de projets.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb))
                return ConversationHandler.END
            elif limit == config.MAX_BOTS_PRO:
                await msg.reply_text(
                    f"⛔ <b>Limite Pro atteinte : {config.MAX_BOTS_PRO} projets.</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏠 Retour", callback_data="back_home")
                    ]]))
                return ConversationHandler.END

    # Générer URL pour les sites web (via le proxy /site/<tid>/<slug>/)
    website_url = None
    if deploy_type == "website":
        safe_slug = "".join(
            c if c.isalnum() or c == "_" else "_"
            for c in project_name.lower().strip()
        )[:48]
        repl_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
        if repl_domain:
            website_url = f"https://{repl_domain}/site/{tid}/{safe_slug}/"
        else:
            base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
            if base:
                website_url = f"{base}/site/{tid}/{safe_slug}/"

    row = save_bot(tid, project_name, api_token or "", main_code,
                   extra_files, env_vars, nom, prenom,
                   project_type=deploy_type, website_url=website_url)
    pnum     = row["project_number"]
    date_str = row["date_creation"].strftime("%d/%m/%Y à %H:%M")

    extra_info = ""
    if extra_files:
        n = len(extra_files)
        extra_info = f"├ 📎 Fichiers supplémentaires : {n}\n"

    type_label = "🌐 Site Web" if deploy_type == "website" else "🤖 Bot Telegram"

    can_deploy = is_admin(tid) or is_subscription_active(tid)
    if can_deploy:
        summary_header = (
            f"✅ <b>{type_label} « {project_name} » enregistré !</b>\n\n"
            f"├ 📁 Projet N°{pnum}\n"
            f"├ 📅 {date_str}\n"
            + (f"├ 🔑 Token : <code>{api_token[:15]}...</code>\n" if api_token else "")
            + (f"├ 🌐 URL : {website_url}\n" if website_url else "")
            + extra_info
            + "\n⏳ <b>Démarrage en cours...</b>"
        )
        await msg.reply_text(summary_header, parse_mode="HTML")
        success, start_msg = await asyncio.to_thread(start_user_bot, tid, project_name)
        if success:
            log_activity(tid, "project_start", f"{deploy_type}:{project_name}")
        kb_rows = [
            [InlineKeyboardButton(f"⛔ Arrêter {project_name}", callback_data=f"stop:{project_name}")
             if success else
             InlineKeyboardButton("🔄 Réessayer", callback_data=f"deploy:{project_name}")],
            [InlineKeyboardButton("📋 Mes projets", callback_data="my_bots_list")],
            [InlineKeyboardButton("➕ Héberger un autre projet", callback_data="add_bot")],
            [InlineKeyboardButton("🏠 Menu principal", callback_data="back_home")],
        ]
        if deploy_type == "website" and website_url:
            kb_rows.insert(1, [InlineKeyboardButton("🌐 Voir le site", url=website_url)])
        await msg.reply_text(start_msg, parse_mode="Markdown",
                             reply_markup=InlineKeyboardMarkup(kb_rows))
    else:
        summary = (
            f"✅ <b>{type_label} « {project_name} » enregistré !</b>\n\n"
            f"├ 📁 Projet N°{pnum}\n"
            f"├ 📅 {date_str}\n"
            + (f"├ 🔑 Token : <code>{api_token[:15]}...</code>\n" if api_token else "")
            + extra_info
            + "\n⚠️ <b>Abonnement requis pour démarrer.</b>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Payer pour démarrer",       callback_data="payer_info")],
            [InlineKeyboardButton("📋 Mes projets",               callback_data="my_bots_list")],
            [InlineKeyboardButton("➕ Héberger un autre projet",   callback_data="add_bot")],
            [InlineKeyboardButton("🏠 Menu principal",            callback_data="back_home")],
        ])
        await msg.reply_text(summary, parse_mode="HTML", reply_markup=kb)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Retour à l'accueil", callback_data="back_home")]])
    await update.message.reply_text(
        "❌ Configuration annulée.",
        reply_markup=kb)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS — Déploiement / Arrêt
# ══════════════════════════════════════════════════════════════════════════════

async def deploy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    tid  = update.effective_user.id
    pname = q.data.split(":", 1)[1] if ":" in q.data else None

    if not is_subscription_active(tid) and not is_admin(tid):
        profile  = get_user_profile(tid)
        msg, kb  = _red_panel(profile or {})
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
        return

    await q.edit_message_text("⏳ *Démarrage en cours...*", parse_mode="Markdown")
    success, message = await asyncio.to_thread(start_user_bot, tid, pname)
    if success:
        log_activity(tid, "bot_start", pname)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⛔ Arrêter {pname}", callback_data=f"stop:{pname}")],
            [InlineKeyboardButton("📋 Mes bots", callback_data="my_bots_list")],
        ])
    else:
        log_activity(tid, "bot_start_fail", f"{pname}: {message[:200]}")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Réessayer", callback_data=f"deploy:{pname}")
        ]])
    await q.edit_message_text(message, parse_mode="Markdown", reply_markup=kb)

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    await q.answer()
    tid   = update.effective_user.id
    pname = q.data.split(":", 1)[1] if ":" in q.data else None
    success, message = stop_user_bot(tid, pname)
    if success:
        log_activity(tid, "bot_stop", pname or "")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🚀 Relancer {pname}", callback_data=f"deploy:{pname}")
    ]])
    await q.edit_message_text(message, parse_mode="Markdown", reply_markup=kb)


# ── Suppression d'un bot (avec confirmation) ────────────────────────────────

async def del_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande de confirmation avant suppression."""
    q     = update.callback_query
    await q.answer()
    pname = q.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Oui, supprimer définitivement", callback_data=f"del_yes:{pname}")],
        [InlineKeyboardButton("❌ Annuler",                        callback_data="del_no")],
    ])
    await q.edit_message_text(
        f"⚠️ *Confirmer la suppression*\n\n"
        f"Vous êtes sur le point de supprimer le bot :\n"
        f"*« {pname} »*\n\n"
        "Cette action est *irréversible* — le bot sera arrêté et toutes ses données supprimées.",
        parse_mode="Markdown",
        reply_markup=kb)

async def del_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirme et effectue la suppression."""
    q     = update.callback_query
    await q.answer()
    tid   = update.effective_user.id
    pname = q.data.split(":", 1)[1]
    # 1. Arrêter le bot si en cours d'exécution
    stop_user_bot(tid, pname)
    # 2. Supprimer le fichier .py sur disque (dans user_bots/)
    import re as _re
    safe_slug = _re.sub(r"[^a-z0-9]+", "_", pname.lower().strip())[:24]
    bot_file = os.path.join(
        os.path.dirname(__file__), "user_bots",
        f"bot_{tid}_{safe_slug}.py"
    )
    if os.path.exists(bot_file):
        os.remove(bot_file)
    # 3. Supprimer le ZIP sauvegardé
    safe_zip = "".join(c if c.isalnum() or c == "_" else "_" for c in pname.lower())
    zip_file = os.path.join(
        os.path.dirname(__file__), "uploads",
        f"{tid}_{safe_zip}.zip"
    )
    if os.path.exists(zip_file):
        os.remove(zip_file)
    # 4. Supprimer de la base de données
    delete_bot(tid, pname)
    # 5. Retour au panel (envoyer un nouveau message pour éviter conflit d'édition)
    kb2 = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Retour à l'accueil", callback_data="back_home")]])
    await q.edit_message_text(
        f"🗑 <b>Bot « {pname} » supprimé.</b>\n\n"
        "Appuyez sur le bouton ci-dessous pour retourner à l'accueil.",
        parse_mode="HTML", reply_markup=kb2)

async def del_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annule la suppression, retourne au panel."""
    q = update.callback_query
    await q.answer("Suppression annulée.")
    await back_home_callback(update, context)

async def my_bots_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    await back_home_callback(update, context)


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK — Modifier un projet (mise à jour du code)
# ══════════════════════════════════════════════════════════════════════════════

async def modify_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'utilisateur clique sur ✏️ → déclenche la mise à jour du ZIP."""
    q     = update.callback_query
    await q.answer()
    pname = q.data.split(":", 1)[1]
    tid   = update.effective_user.id
    bot_row = get_bot(tid, pname)
    if not bot_row:
        await q.edit_message_text("❌ Projet introuvable.",
                                  reply_markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("🏠 Retour", callback_data="back_home")
                                  ]]))
        return ConversationHandler.END
    context.user_data["project_name"] = pname
    context.user_data["modify_mode"]  = True
    context.user_data["deploy_type"]  = bot_row.get("project_type", "bot")
    ptype_label = "🌐 site web" if bot_row.get("project_type") == "website" else "🤖 bot"
    await q.edit_message_text(
        f"✏️ <b>Modifier le projet « {pname} »</b>\n\n"
        f"Type : {ptype_label}\n\n"
        "📦 Envoyez le <b>nouveau fichier ZIP</b> pour mettre à jour le code.\n\n"
        "<i>Le token et le type de projet resteront inchangés.\n"
        "Seuls le code source et les fichiers seront mis à jour.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Annuler", callback_data="back_home")
        ]]))
    return MODIFY_ZIP_STEP


async def get_modify_zip_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit le nouveau ZIP pour la mise à jour du code."""
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Envoyez un fichier `.zip`.", parse_mode="Markdown")
        return MODIFY_ZIP_STEP
    if not (doc.file_name or "").lower().endswith(".zip"):
        await update.message.reply_text(f"❌ `{doc.file_name}` n'est pas un ZIP.",
                                        parse_mode="Markdown")
        return MODIFY_ZIP_STEP

    tid   = update.effective_user.id
    pname = context.user_data.get("project_name", "")
    if not pname:
        await update.message.reply_text("❌ Session expirée. Tapez /start.")
        return ConversationHandler.END

    wait = await update.message.reply_text("⬇️ *Téléchargement et analyse...*",
                                           parse_mode="Markdown")
    zip_bytes = await (await doc.get_file()).download_as_bytearray()

    # Sauvegarder le ZIP
    safe_save  = "".join(c if c.isalnum() or c == "_" else "_" for c in pname.lower())
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    with open(os.path.join(upload_dir, f"{tid}_{safe_save}.zip"), "wb") as _zf:
        _zf.write(zip_bytes)

    py_files:    dict[str, str] = {}
    extra_files: dict[str, str] = {}
    env_vars:    dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.endswith("/") or name.startswith("__MACOSX"):
                    continue
                basename = os.path.basename(name)
                if not basename:
                    continue
                ext = os.path.splitext(basename)[1].lower()
                if basename in (".env", "env"):
                    for line in zf.read(name).decode("utf-8", errors="replace").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        env_vars[k.strip()] = v.strip().strip('"').strip("'")
                elif basename.endswith(".py"):
                    try:
                        py_files[basename] = zf.read(name).decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                elif ext in TEXT_EXTENSIONS:
                    try:
                        extra_files[basename] = zf.read(name).decode("utf-8", errors="replace")
                    except Exception:
                        pass
    except zipfile.BadZipFile:
        await wait.edit_text("❌ ZIP invalide.")
        return MODIFY_ZIP_STEP

    MAIN_PRIORITY = ["main.py", "bot.py", "app.py", "index.py", "run.py", "start.py"]
    main_filename = None
    for candidate in MAIN_PRIORITY:
        if candidate in py_files:
            main_filename = candidate
            break
    if main_filename is None and py_files:
        main_filename = sorted(py_files.keys())[0]
    if main_filename is None:
        await wait.edit_text("❌ Aucun fichier Python trouvé dans le ZIP.")
        return MODIFY_ZIP_STEP

    main_code = py_files.pop(main_filename)
    extra_files.update(py_files)

    # Analyse syntaxe
    all_py = {"main.py": main_code}
    all_py.update({k: v for k, v in extra_files.items() if k.endswith(".py")})
    syntax_errors = _analyze_py_syntax(all_py)
    _, pip_deps   = detect_local_dependencies(main_code)
    pkg_str = ", ".join(pip_deps) if pip_deps else "aucun"

    report = (
        f"🔍 <b>Analyse du nouveau code</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📁 Fichiers : {len(extra_files)+1}\n"
        f"📦 Packages : {pkg_str}\n"
    )
    if syntax_errors:
        report += "\n⚠️ <b>Erreurs syntaxe :</b>\n" + "".join(f"  {e}\n" for e in syntax_errors[:5])
    else:
        report += "\n✅ <b>Syntaxe : OK</b>"
    await wait.edit_text(report, parse_mode="HTML")

    # Arrêter le bot, mettre à jour le code, relancer
    stop_user_bot(tid, pname)
    update_bot_code(tid, pname, main_code, extra_files, env_vars or None)
    log_activity(tid, "bot_update", pname)

    await update.effective_message.reply_text(
        f"✅ <b>Projet « {pname} » mis à jour !</b>\n\n"
        "⏳ Relancement en cours...",
        parse_mode="HTML")

    success, start_msg = await asyncio.to_thread(start_user_bot, tid, pname)
    if success:
        log_activity(tid, "bot_start", pname)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⛔ Arrêter {pname}", callback_data=f"stop:{pname}")
         if success else
         InlineKeyboardButton("🔄 Réessayer", callback_data=f"deploy:{pname}")],
        [InlineKeyboardButton("📋 Mes projets", callback_data="my_bots_list")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="back_home")],
    ])
    await update.effective_message.reply_text(start_msg, parse_mode="Markdown",
                                              reply_markup=kb)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS — Paiement utilisateur
# ══════════════════════════════════════════════════════════════════════════════

async def payer_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    tid = update.effective_user.id
    nb  = count_user_bots(tid)
    include_pro = (nb >= config.MAX_BOTS_BASIC) and not is_pro_active(tid)
    await q.message.reply_text(
        "💳 *Choisissez la durée de votre abonnement :*\n\n"
        + "\n".join(f"• {d['label']} — *{d['price']} F*" for d in get_durations()),
        parse_mode="Markdown",
        reply_markup=_dur_keyboard(include_pro=include_pro),
    )

async def pay_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    await q.answer()
    tid   = update.effective_user.id
    hours = int(q.data.split(":")[1])
    dur   = next((d for d in get_durations() if d["hours"] == hours), None)
    if not dur:
        await q.edit_message_text("❌ Durée invalide.")
        return
    _pending_payments[tid] = {"hours": hours, "price": dur["price"], "label": dur["label"], "is_pro": False}
    payment_info = get_setting("payment_info", config.DEFAULT_PAYMENT_INFO)
    await q.edit_message_text(
        f"💳 *Commande :*\n\n"
        f"├ ⏱ Durée : *{dur['label']}*\n"
        f"└ 💰 Montant : *{dur['price']} F*\n\n"
        f"{payment_info}\n\n"
        "📸 *Envoyez maintenant une capture d'écran de votre paiement.*",
        parse_mode="Markdown")

async def pay_pro_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    await q.answer()
    tid   = update.effective_user.id
    price = get_pro_price()
    _pending_payments[tid] = {"hours": 168, "price": price, "label": "Pro 7 jours", "is_pro": True}
    payment_info = get_setting("payment_info", config.DEFAULT_PAYMENT_INFO)
    await q.edit_message_text(
        f"⭐ *Abonnement Pro*\n\n"
        f"├ 🤖 {config.MAX_BOTS_PRO} bots simultanés\n"
        f"├ ⏱ Durée : *7 jours*\n"
        f"└ 💰 Montant : *{price} F*\n\n"
        f"{payment_info}\n\n"
        "📸 *Envoyez votre capture d'écran de paiement.*",
        parse_mode="Markdown")

async def pay_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _pending_payments.pop(update.effective_user.id, None)
    await q.edit_message_text("❌ Paiement annulé.")


# ══════════════════════════════════════════════════════════════════════════════
# CAPTURE SCREENSHOT DE PAIEMENT
# ══════════════════════════════════════════════════════════════════════════════

async def payment_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if tid not in _pending_payments:
        return
    info    = _pending_payments.pop(tid)
    hours   = info["hours"]
    price   = info["price"]
    label   = info["label"]
    is_pro  = info.get("is_pro", False)
    profile = get_user_profile(tid)
    user    = update.effective_user
    nom_d   = f"{profile['prenom']} {profile['nom']}" if profile and profile.get("nom") else user.full_name

    log_activity(tid, "payment_submitted", f"{label} ({price} F)")
    type_label = "⭐ Pro" if is_pro else "Standard"
    notif_text = (
        f"📸 *Preuve de paiement*\n\n"
        f"├ 👤 {nom_d}\n"
        f"├ 🆔 `{tid}`\n"
        f"├ 💳 Plan : *{type_label} — {label}*\n"
        f"├ 💰 {price} F\n"
        f"└ 📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    val_cb = f"pay_ok:{tid}:{hours}:{'1' if is_pro else '0'}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Valider {label}", callback_data=val_cb)],
        [InlineKeyboardButton("❌ Refuser",           callback_data=f"pay_no:{tid}")],
    ])
    for admin_id in config.ADMIN_TELEGRAM_IDS:
        try:
            if update.message.photo:
                await update.get_bot().send_photo(
                    admin_id, update.message.photo[-1].file_id,
                    caption=notif_text, parse_mode="Markdown", reply_markup=kb)
            elif update.message.document:
                await update.get_bot().send_document(
                    admin_id, update.message.document.file_id,
                    caption=notif_text, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.warning(f"Admin notify {admin_id}: {e}")
    await update.message.reply_text(
        "✅ *Capture reçue !*\n\nTransmise à l'administrateur. Activation sous peu. ⏳",
        parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATION ADMIN via boutons inline sur la photo
# ══════════════════════════════════════════════════════════════════════════════

async def pay_validate_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return

    parts    = q.data.split(":")     # pay_ok:tid:hours:is_pro
    user_tid = int(parts[1])
    hours    = int(parts[2])
    is_pro   = parts[3] == "1" if len(parts) > 3 else False

    if is_pro:
        weeks   = max(1, hours // 168)
        sub_end = set_pro_subscription(user_tid, weeks)
        label   = f"Pro {weeks} semaine(s)"
        log_activity(user_tid, "subscription_pro", f"{weeks} semaine(s)")
    else:
        sub_end = set_subscription(user_tid, hours)
        label   = f"{hours}h"
        log_activity(user_tid, "subscription_activated", f"{hours}h")

    exp_str = _sub_expire_str(sub_end) if sub_end else "—"
    await q.edit_message_caption(
        (q.message.caption or "") + f"\n\n✅ *Validé* — {label} — expire le {exp_str}",
        parse_mode="Markdown")

    profile = get_user_profile(user_tid)
    remaining = _sub_remaining_str(sub_end) if sub_end else "—"
    ico  = "⭐" if is_pro else "✅"
    plan = "Pro" if is_pro else "Standard"
    try:
        bots = get_user_bots(user_tid)
        deploy_row = [[InlineKeyboardButton(
            f"🚀 Démarrer {b['project_name']}",
            callback_data=f"deploy:{b['project_name']}")] for b in bots] if bots else []
        deploy_row += [[InlineKeyboardButton("📋 Mes bots", callback_data="my_bots_list")]]
        await update.get_bot().send_message(user_tid,
            f"🔵 *{ico} Abonnement {plan} activé !*\n\n"
            f"├ ⏳ Temps restant : *{remaining}*\n"
            f"└ 📅 Expire le : {exp_str}\n\n"
            "Tapez /monbot pour accéder à votre tableau de bord. 🚀",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(deploy_row))
    except Exception as e:
        logger.warning(f"Blue panel notify {user_tid}: {e}")

async def pay_refuse_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return
    user_tid = int(q.data.split(":")[1])
    await q.edit_message_caption(
        (q.message.caption or "") + "\n\n❌ *Refusé par l'admin.*",
        parse_mode="Markdown")
    try:
        await update.get_bot().send_message(user_tid,
            "❌ *Paiement refusé.*\n\nContactez l'administrateur ou tapez /payer pour réessayer.",
            parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Refuse notify {user_tid}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Panel utilisateurs
# ══════════════════════════════════════════════════════════════════════════════

async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé", show_alert=True); return

    profiles = get_all_profiles()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="back_home")]])

    if not profiles:
        await q.edit_message_text("Aucun utilisateur enregistré.", reply_markup=kb)
        return

    # Filtrer les admins de la liste affichée
    users = [p for p in profiles if p["telegram_id"] not in config.ADMIN_TELEGRAM_IDS]

    header = f"👥 <b>Utilisateurs ({len(users)})</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    blocks   = []
    dl_rows  = []   # boutons de téléchargement accumulés pour le clavier

    for p in users:
        u_tid   = p["telegram_id"]
        active  = is_subscription_active(u_tid)
        pro     = is_pro_active(u_tid)
        trial   = p.get("trial_used", False)
        bots    = get_user_bots(u_tid)
        nb      = len(bots)
        sub_end = p.get("subscription_end")
        pro_end = p.get("pro_subscription_end")

        # Statut abonnement
        if pro and pro_end:
            statut = f"⭐ Pro — <b>{_sub_remaining_str(pro_end)}</b> restant"
        elif active and sub_end:
            statut = f"✅ Actif — <b>{_sub_remaining_str(sub_end)}</b> restant"
        elif sub_end:
            statut = "⛔ Expiré (le " + _sub_expire_str(sub_end) + ")"
        elif trial:
            statut = "🎁 Essai utilisé"
        else:
            statut = "🆕 Aucun abonnement"

        # Liste des bots avec fichiers
        if bots:
            bot_desc_lines = []
            for b in bots:
                ico   = "🟢" if b["is_running"] else "🔴"
                extra = b.get("extra_files") or {}
                if isinstance(extra, str):
                    try:    extra = json.loads(extra)
                    except: extra = {}
                n_files   = len(extra)
                file_info = f" +{n_files}f" if n_files else ""
                bot_desc_lines.append(
                    f"    {ico} <code>{b['project_name']}</code>{file_info}"
                )
                # Bouton téléchargement pour ce bot
                pname_short = b["project_name"][:20]   # limite callback_data 64B
                dl_cb = f"adl:{u_tid}:{pname_short}"
                lbl   = f"📥 {pname_short} ({u_tid})"[:32]
                dl_rows.append([InlineKeyboardButton(lbl, callback_data=dl_cb)])
            bots_lines = "\n".join(bot_desc_lines)
        else:
            bots_lines = "    <i>Aucun bot</i>"

        name = f"{p.get('prenom', '')} {p.get('nom', '')}".strip() or "Sans nom"
        blocks.append(
            f"👤 <b>{name}</b>\n"
            f"   ID : <code>{u_tid}</code> | {nb} bot(s)\n"
            f"   {statut}\n"
            f"{bots_lines}"
        )

    # Boutons d'action par utilisateur
    action_rows = []
    for p in users:
        u_tid   = p["telegram_id"]
        blocked = bool(p.get("is_blocked", False))
        name    = f"{p.get('prenom', '')} {p.get('nom', '')}".strip() or str(u_tid)
        name_s  = name[:18]
        block_lbl = "🔓 Débloquer" if blocked else "🔒 Bloquer"
        block_cb  = f"admin_unblock:{u_tid}" if blocked else f"admin_block:{u_tid}"
        action_rows.append([
            InlineKeyboardButton(f"🗑 {name_s}", callback_data=f"admin_del_ask:{u_tid}"),
            InlineKeyboardButton(block_lbl,      callback_data=block_cb),
        ])

    # Clavier final : boutons DL + actions + retour
    kb_rows  = dl_rows + action_rows + [[InlineKeyboardButton("🔙 Retour", callback_data="back_home")]]
    kb_final = InlineKeyboardMarkup(kb_rows)

    # Découper en messages de max 3900 chars
    full_text = header + "\n\n".join(blocks)
    if len(full_text) <= 3900:
        await q.edit_message_text(full_text, parse_mode="HTML", reply_markup=kb_final)
    else:
        await q.edit_message_text(header + "⏳ Chargement...", parse_mode="HTML")
        current = header
        msgs = []
        for block in blocks:
            if len(current) + len(block) + 4 > 3900:
                msgs.append(current)
                current = ""
            current += block + "\n\n"
        if current:
            msgs.append(current)
        for i, msg_text in enumerate(msgs):
            rm = kb_final if i == len(msgs) - 1 else None
            await q.message.reply_text(msg_text.strip(), parse_mode="HTML", reply_markup=rm)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Actions sur les utilisateurs (bloquer, supprimer)
# ══════════════════════════════════════════════════════════════════════════════

async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Panel statistiques de déploiement pour l'admin."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return

    try:
        s = get_deployment_stats()
    except Exception as e:
        await q.edit_message_text(f"❌ Erreur lors de la récupération : {e}",
                                  reply_markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("🔙 Retour", callback_data="back_home")
                                  ]]))
        return

    # Compter les projets actifs en temps réel
    all_bots    = get_all_bots()
    running_now = [b for b in all_bots if b["is_running"]]
    running_bots_now = sum(1 for b in running_now if b.get("project_type", "bot") == "bot")
    running_web_now  = sum(1 for b in running_now if b.get("project_type", "bot") == "website")

    text = (
        "📊 <b>Panneau de Statistiques</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👥 <b>Utilisateurs</b>\n"
        f"├ Total inscrits      : <b>{s['total_users']}</b>\n"
        f"└ Abonnements actifs  : <b>{s['active_subscribers']}</b>\n\n"
        "📦 <b>Projets hébergés</b>\n"
        f"├ Total projets       : <b>{s['total_projects']}</b>\n"
        f"├ 🤖 Bots Telegram    : <b>{s['total_bots']}</b>\n"
        f"├ 🌐 Sites Web        : <b>{s['total_websites']}</b>\n"
        f"├ 🟢 En ligne (bots)  : <b>{running_bots_now}</b>\n"
        f"└ 🟢 En ligne (sites) : <b>{running_web_now}</b>\n\n"
        "🚀 <b>Déploiements</b>\n"
        f"├ Total démarrage     : <b>{s['total_deployments']}</b>\n"
        f"└ Dernières 24h       : <b>{s['deployments_today']}</b>\n\n"
        f"<i>Mis à jour : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Actualiser",   callback_data="admin_stats"),
         InlineKeyboardButton("👥 Utilisateurs", callback_data="admin_users")],
        [InlineKeyboardButton("🔙 Retour",       callback_data="back_home")],
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def admin_source_zip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Génère et envoie un ZIP du code source du bot manager à l'admin."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return

    await q.edit_message_text(
        "📦 <b>Génération du ZIP source en cours...</b>",
        parse_mode="HTML")

    # Fichiers à inclure dans le ZIP
    base_dir   = os.path.dirname(os.path.abspath(__file__))
    src_files  = ["bot.py", "db.py", "runner.py", "config.py", "analyzer.py",
                  "web_server.py", "requirements.txt"]
    buf = io.BytesIO()
    ts  = datetime.now().strftime("%Y%m%d_%H%M")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in src_files:
            fpath = os.path.join(base_dir, fname)
            if os.path.exists(fpath):
                zf.write(fpath, fname)
        # README minimal
        readme = (
            "# Bot Manager — Code Source\n\n"
            f"Archive générée le {datetime.now().strftime('%d/%m/%Y à %H:%M')}\n\n"
            "## Fichiers inclus\n"
            + "".join(f"- {f}\n" for f in src_files) +
            "\n## Installation\n"
            "```bash\npip install -r requirements.txt\npython3 bot.py\n```\n"
        )
        zf.writestr("README.md", readme)
    buf.seek(0)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="back_home")]])
    try:
        from telegram import InputFile
        await q.message.reply_document(
            document=InputFile(buf, filename=f"bot_manager_{ts}.zip"),
            caption=f"📦 <b>Code source Bot Manager</b>\n\n"
                    f"Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}\n"
                    f"Fichiers : {len(src_files)}",
            parse_mode="HTML")
        await q.edit_message_text(
            "✅ <b>ZIP envoyé !</b>",
            parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await q.edit_message_text(
            f"❌ Erreur : {e}", parse_mode="HTML", reply_markup=kb)


async def admin_del_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande confirmation avant de supprimer un utilisateur."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return
    u_tid = int(q.data.split(":", 1)[1])
    profile = get_user_profile(u_tid)
    name = f"{profile.get('prenom', '')} {profile.get('nom', '')}".strip() if profile else str(u_tid)
    nb_bots = len(get_user_bots(u_tid))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Oui, supprimer définitivement", callback_data=f"admin_del_yes:{u_tid}")],
        [InlineKeyboardButton("❌ Annuler",                        callback_data="admin_users")],
    ])
    await q.edit_message_text(
        f"⚠️ <b>Supprimer l'utilisateur ?</b>\n\n"
        f"👤 {name}\n"
        f"🆔 <code>{u_tid}</code>\n"
        f"🤖 {nb_bots} bot(s)\n\n"
        "Cette action supprimera son compte ET tous ses bots définitivement.",
        parse_mode="HTML",
        reply_markup=kb)

async def admin_del_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirme la suppression de l'utilisateur."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return
    u_tid = int(q.data.split(":", 1)[1])
    for b in get_user_bots(u_tid):
        if b["is_running"]:
            stop_user_bot(u_tid, b["project_name"])
    delete_user(u_tid)
    log_activity(update.effective_user.id, "admin_del_user", str(u_tid))
    try:
        await update.get_bot().send_message(u_tid,
            "🗑️ *Votre compte a été supprimé par l'administrateur.*",
            parse_mode="Markdown")
    except Exception:
        pass
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour utilisateurs", callback_data="admin_users")]])
    await q.edit_message_text(
        f"🗑️ <b>Utilisateur <code>{u_tid}</code> supprimé.</b>",
        parse_mode="HTML", reply_markup=kb)

async def admin_block_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bloque un utilisateur."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return
    u_tid = int(q.data.split(":", 1)[1])
    block_user(u_tid)
    # Arrêter ses bots
    for b in get_user_bots(u_tid):
        if b["is_running"]:
            stop_user_bot(u_tid, b["project_name"])
    log_activity(update.effective_user.id, "admin_block_user", str(u_tid))
    try:
        await update.get_bot().send_message(u_tid,
            "🚫 *Votre compte a été bloqué par l'administrateur.*\nContactez le support.",
            parse_mode="Markdown")
    except Exception:
        pass
    await q.answer(f"✅ Utilisateur {u_tid} bloqué.", show_alert=True)
    await admin_users_callback(update, context)

async def admin_unblock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Débloque un utilisateur."""
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌ Accès refusé.", show_alert=True); return
    u_tid = int(q.data.split(":", 1)[1])
    unblock_user(u_tid)
    log_activity(update.effective_user.id, "admin_unblock_user", str(u_tid))
    try:
        await update.get_bot().send_message(u_tid,
            "✅ *Votre compte a été débloqué. Vous pouvez de nouveau utiliser le service.*",
            parse_mode="Markdown")
    except Exception:
        pass
    await q.answer(f"✅ Utilisateur {u_tid} débloqué.", show_alert=True)
    await admin_users_callback(update, context)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Configuration des paiements (conversation)
# ══════════════════════════════════════════════════════════════════════════════

async def admin_pay_config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("❌", show_alert=True)
        return ConversationHandler.END

    current_info  = get_setting("payment_info",       config.DEFAULT_PAYMENT_INFO)
    current_price = get_setting("price_7_days",       str(config.DEFAULT_PRICE_7_DAYS))
    current_pro   = get_setting("pro_price_per_week", str(config.DEFAULT_PRO_PRICE_PER_WEEK))

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Modifier les instructions",  callback_data="admin_pay_edit_info")],
        [InlineKeyboardButton("💰 Modifier le prix 7 jours",   callback_data="admin_pay_edit_price")],
        [InlineKeyboardButton("⭐ Modifier le prix Pro/sem",   callback_data="admin_pay_edit_pro")],
        [InlineKeyboardButton("❌ Fermer", callback_data="admin_pay_close")],
    ])
    await q.edit_message_text(
        f"⚙️ *Configuration des paiements*\n\n"
        f"━━ *Instructions actuelles :* ━━\n{current_info}\n\n"
        f"━━ *Tarifs actuels :* ━━\n"
        f"• 7 jours Standard : *{current_price} F*\n"
        f"• 7 jours Pro : *{current_pro} F/sem*\n\n"
        "_Les durées 1h, 24h, 72h sont calculées proportionnellement au prix 7 jours._",
        parse_mode="Markdown", reply_markup=kb)
    return ADMIN_PAY_MENU

async def admin_pay_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "admin_pay_edit_info":
        await q.edit_message_text(
            "✏️ *Entrez les nouvelles instructions de paiement :*\n\n"
            "_Exemple : 'Envoyez votre paiement au +225 XX XX XX XX via Wave.'_",
            parse_mode="Markdown")
        return ADMIN_PAY_EDIT_INFO
    elif data == "admin_pay_edit_price":
        await q.edit_message_text(
            "💰 *Entrez le nouveau prix pour 7 jours (en F) :*\n\n"
            "_Exemple : 1500_",
            parse_mode="Markdown")
        context.user_data["admin_pay_editing"] = "price_7_days"
        return ADMIN_PAY_EDIT_PRICE
    elif data == "admin_pay_edit_pro":
        await q.edit_message_text(
            "⭐ *Entrez le nouveau prix Pro/semaine (en F) :*\n\n"
            "_Exemple : 2500_",
            parse_mode="Markdown")
        context.user_data["admin_pay_editing"] = "pro_price_per_week"
        return ADMIN_PAY_EDIT_PRICE
    elif data == "admin_pay_close":
        await q.edit_message_text("✅ Configuration fermée.")
        return ConversationHandler.END
    return ADMIN_PAY_MENU

async def admin_pay_edit_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_info = update.message.text.strip()
    set_setting("payment_info", new_info)
    await update.message.reply_text("✅ *Instructions de paiement mises à jour !*", parse_mode="Markdown")
    return ConversationHandler.END

async def admin_pay_edit_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not val.isdigit():
        await update.message.reply_text("❌ Entrez un nombre entier. Réessayez :")
        return ADMIN_PAY_EDIT_PRICE
    key = context.user_data.get("admin_pay_editing", "price_7_days")
    set_setting(key, val)
    label = "Prix Pro/sem" if "pro" in key else "Prix 7 jours"
    await update.message.reply_text(f"✅ *{label} mis à jour : {val} F*", parse_mode="Markdown")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDES UTILISATEUR
# ══════════════════════════════════════════════════════════════════════════════

async def my_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    if is_subscription_active(tid) or is_admin(tid):
        msg, kb = _blue_panel(tid)
    else:
        profile = get_user_profile(tid)
        if not profile:
            await update.message.reply_text("❌ Aucun compte. Tapez /start.")
            return
        msg, kb = _red_panel(profile)
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)

async def abonnement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await my_bot_command(update, context)

async def payer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    nb  = count_user_bots(tid)
    include_pro = (nb >= config.MAX_BOTS_BASIC) and not is_pro_active(tid)
    await update.message.reply_text(
        "💳 *Choisissez la durée :*\n\n"
        + "\n".join(f"• {d['label']} — *{d['price']} F*" for d in get_durations()),
        parse_mode="Markdown",
        reply_markup=_dur_keyboard(include_pro=include_pro))


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDES ADMIN
# ══════════════════════════════════════════════════════════════════════════════

async def activer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/activer <tid> <jours>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Usage : `/activer <telegram_id> <jours>`", parse_mode="Markdown"); return
    tid, days = int(args[0]), int(args[1])
    sub_end = set_subscription_days(tid, days)
    exp_str = _sub_expire_str(sub_end) if sub_end else "—"
    profile = get_user_profile(tid)
    nom_d   = f"{profile['prenom']} {profile['nom']}" if profile and profile.get("nom") else str(tid)
    await update.message.reply_text(
        f"✅ *Activé* pour `{tid}` ({nom_d})\n└ {days}j — expire le {exp_str}",
        parse_mode="Markdown")
    remaining = _sub_remaining_str(sub_end) if sub_end else "—"
    try:
        bots = get_user_bots(tid)
        rows = [[InlineKeyboardButton(f"🚀 Démarrer {b['project_name']}", callback_data=f"deploy:{b['project_name']}")] for b in bots]
        await update.get_bot().send_message(tid,
            f"🔵 *Abonnement activé !*\n\n"
            f"├ ⏳ Temps restant : *{remaining}*\n└ 📅 Expire le : {exp_str}\n\nTapez /monbot 🚀",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows) if rows else None)
    except Exception as e:
        logger.warning(e)

async def activer_pro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/activer_pro <tid> <semaines>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Usage : `/activer_pro <tid> <semaines>`", parse_mode="Markdown"); return
    tid, weeks = int(args[0]), int(args[1])
    pro_end = set_pro_subscription(tid, weeks)
    exp_str = _sub_expire_str(pro_end) if pro_end else "—"
    await update.message.reply_text(f"⭐ *Pro activé* pour `{tid}` — {weeks} sem. — expire le {exp_str}", parse_mode="Markdown")
    try:
        await update.get_bot().send_message(tid,
            f"⭐ *Plan Pro activé !*\n\n"
            f"├ 🤖 {config.MAX_BOTS_PRO} bots simultanés\n└ 📅 Expire le : {exp_str}",
            parse_mode="Markdown")
    except Exception as e:
        logger.warning(e)

async def suspendre_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/suspendre <tid>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage : `/suspendre <tid>`", parse_mode="Markdown"); return
    tid = int(args[0])
    revoke_subscription(tid)
    for b in get_user_bots(tid):
        if b["is_running"]:
            stop_user_bot(tid, b["project_name"])
    await update.message.reply_text(f"⛔ Suspendu : `{tid}`", parse_mode="Markdown")
    try:
        await update.get_bot().send_message(tid,
            "⛔ *Hébergement suspendu.*\nContactez l'admin ou tapez /payer.",
            parse_mode="Markdown")
    except Exception as e:
        logger.warning(e)

async def supprimer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/supprimer <tid>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage : `/supprimer <tid>`", parse_mode="Markdown"); return
    tid = int(args[0])
    for b in get_user_bots(tid):
        if b["is_running"]:
            stop_user_bot(tid, b["project_name"])
    delete_user(tid)
    await update.message.reply_text(f"🗑️ Compte `{tid}` supprimé.", parse_mode="Markdown")
    try:
        await update.get_bot().send_message(tid, "🗑️ *Votre compte a été supprimé.*\nContactez l'admin.", parse_mode="Markdown")
    except Exception:
        pass

async def stopper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stopper <tid> [project_name]"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage : `/stopper <tid> [nom_projet]`", parse_mode="Markdown"); return
    tid   = int(args[0])
    pname = " ".join(args[1:]) if len(args) > 1 else None
    if pname:
        success, msg = stop_user_bot(tid, pname)
        await update.message.reply_text(f"{'✅' if success else '⚠️'} {msg}", parse_mode="Markdown")
    else:
        for b in get_user_bots(tid):
            if b["is_running"]:
                stop_user_bot(tid, b["project_name"])
        await update.message.reply_text(f"✅ Tous les bots de `{tid}` arrêtés.", parse_mode="Markdown")

async def temps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/temps <tid>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage : `/temps <tid>`", parse_mode="Markdown"); return
    tid = int(args[0])
    profile = get_user_profile(tid)
    if not profile:
        await update.message.reply_text(f"❌ Aucun profil pour `{tid}`.", parse_mode="Markdown"); return
    active  = is_subscription_active(tid)
    sub_end = profile.get("subscription_end")
    pro_end = profile.get("pro_subscription_end")
    sub_str = (f"🟢 Actif — {_sub_remaining_str(sub_end)}" if active
               else f"🔴 {'Expiré' if sub_end else 'Aucun'}")
    pro_str = (f"⭐ Actif — {_sub_remaining_str(pro_end)}" if is_pro_active(tid)
               else ("⭐ Expiré" if pro_end else "—"))
    bots = get_user_bots(tid)
    bots_str = "\n".join(f"   {'🟢' if b['is_running'] else '🔴'} {b['project_name']}" for b in bots) or "  Aucun"
    await update.message.reply_text(
        f"📊 *`{tid}`* — {profile.get('prenom','')} {profile.get('nom','')}\n\n"
        f"├ 💳 {sub_str}\n├ {pro_str}\n└ Bots :\n{bots_str}",
        parse_mode="Markdown")

async def utilisateurs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/utilisateurs"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    profiles = get_all_profiles()
    if not profiles:
        await update.message.reply_text("Aucun utilisateur."); return
    lines = [f"👥 *{len(profiles)} utilisateurs :*\n"]
    for p in profiles:
        tid    = p["telegram_id"]
        active = is_subscription_active(tid)
        nb     = count_user_bots(tid)
        rem    = _sub_remaining_str(p["subscription_end"]) if active and p.get("subscription_end") else "aucun"
        lines.append(f"{'✅' if active else '⛔'} *{p.get('prenom','')} {p.get('nom','')}*\n"
                     f"   `{tid}` — {nb} bots — _{rem}_")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def dbinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dbinfo"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Accès refusé."); return
    import re
    masked = re.sub(r'(:)([^:@]+)(@)', r'\1****\3', config.DATABASE_URL or "—")
    await update.message.reply_text(
        f"🗄️ *DB :* `{masked}`\n"
        f"🌐 *Port :* `{config.PORT}`\n"
        f"🔑 *Token dashboard :* `{config.DASHBOARD_SECRET}`",
        parse_mode="Markdown")

# ── Helper partagé : construit le ZIP d'un projet en mémoire ─────────────────

def _build_project_zip(bot_row: dict, target_tid: int) -> tuple[io.BytesIO, str, str]:
    """Retourne (buf, filename, caption) pour un document Telegram."""
    pname = bot_row["project_name"]

    extra = bot_row.get("extra_files") or {}
    if isinstance(extra, str):
        try:    extra = json.loads(extra)
        except: extra = {}

    env_vars = bot_row.get("env_vars") or {}
    if isinstance(env_vars, str):
        try:    env_vars = json.loads(env_vars)
        except: env_vars = {}

    main_py = bot_row.get("main_py") or ""
    n_extra = len(extra)
    statut  = "🟢 actif" if bot_row.get("is_running") else "🔴 arrêté"

    meta = (
        f"# Projet    : {pname}\n"
        f"# UserID    : {target_tid}\n"
        f"# Token     : {bot_row.get('api_token','???')}\n"
        f"# Créé le   : {bot_row.get('date_creation','—')}\n"
        f"# Statut    : {statut}\n"
        f"# Fichiers  : main.py"
        + (", " + ", ".join(extra.keys()) if extra else "")
        + "\n\n# Variables d'environnement :\n"
        + "\n".join(f"# {k}={v}" for k, v in env_vars.items())
        + "\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", main_py)
        for fname, content in extra.items():
            if isinstance(content, str):
                zf.writestr(fname, content)
        zf.writestr("_META.txt", meta)
    buf.seek(0)

    safe   = "".join(c if c.isalnum() or c in "-_." else "_" for c in pname)
    fname  = f"{safe}_{target_tid}.zip"
    caption = (
        f"📦 <b>{pname}</b> — user <code>{target_tid}</code>\n"
        f"📁 <code>main.py</code>"
        + (f" + {n_extra} fichier{'s' if n_extra > 1 else ''} supplémentaire{'s' if n_extra > 1 else ''}" if n_extra else "")
        + f"\n🔑 <code>_META.txt</code> (token + variables)"
    )
    return buf, fname, caption


async def dl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dl <telegram_id> [projet] — Télécharge le code d'un bot utilisateur."""
    tid = update.effective_user.id
    if not is_admin(tid):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📥 <b>Usage :</b>\n"
            "<code>/dl &lt;telegram_id&gt;</code>  → 1er bot\n"
            "<code>/dl &lt;telegram_id&gt; &lt;nom_projet&gt;</code>  → projet précis\n\n"
            "💡 <b>Exemple :</b> <code>/dl 123456789 MonBot</code>",
            parse_mode="HTML"); return
    try:
        target_tid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide (doit être un nombre)."); return

    pname  = " ".join(args[1:]).strip() if len(args) > 1 else None
    bots   = get_user_bots(target_tid)
    if not bots:
        await update.message.reply_text(
            f"⚠️ Aucun bot pour <code>{target_tid}</code>.", parse_mode="HTML"); return

    if pname:
        bot_row = next((b for b in bots if b["project_name"].lower() == pname.lower()), None)
        if not bot_row:
            names = ", ".join(f"<code>{b['project_name']}</code>" for b in bots)
            await update.message.reply_text(
                f"❌ Projet « {pname} » introuvable.\nDisponibles : {names}",
                parse_mode="HTML"); return
    else:
        bot_row = bots[0]

    buf, fname, caption = _build_project_zip(bot_row, target_tid)
    await update.message.reply_document(
        document=buf, filename=fname, caption=caption, parse_mode="HTML")
    log_activity(tid, "admin_dl", f"{target_tid}/{bot_row['project_name']}")


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/logs [telegram_id] — Affiche les 50 dernières activités."""
    tid = update.effective_user.id
    if not is_admin(tid):
        await update.message.reply_text("❌ Accès refusé."); return
    args = context.args or []
    target = None
    if args:
        try:
            target = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ ID invalide."); return

    entries = get_activity_logs(telegram_id=target, limit=50)
    if not entries:
        await update.message.reply_text("📋 Aucune activité enregistrée.")
        return
    who = f"pour <code>{target}</code>" if target else "(tous les utilisateurs)"
    lines = [f"📋 <b>Journal d'activité {who}</b> :\n"]
    for e in entries:
        ts  = e["ts"].strftime("%d/%m %H:%M") if e["ts"] else "—"
        act = e["action"]
        det = e["details"]
        lines.append(f"<code>{ts}</code> | <code>{e['telegram_id']}</code> → <b>{act}</b>"
                     + (f"\n  ↳ {det}" if det else ""))
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(tronqué)"
    await update.message.reply_text(text, parse_mode="HTML")


async def admin_dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback adl:{target_tid}:{project_name} — Télécharge le ZIP d'un projet via bouton inline."""
    q   = update.callback_query
    tid = update.effective_user.id
    if not is_admin(tid):
        await q.answer("❌ Accès refusé.", show_alert=True); return
    await q.answer("⏳ Préparation du ZIP…")

    parts = q.data.split(":", 2)   # ["adl", "TARGET_TID", "PROJECT_NAME"]
    if len(parts) < 3:
        await q.message.reply_text("❌ Données invalides."); return

    try:
        target_tid = int(parts[1])
    except ValueError:
        await q.message.reply_text("❌ ID utilisateur invalide."); return

    pname_req = parts[2].strip()
    bots = get_user_bots(target_tid)
    if not bots:
        await q.message.reply_text(
            f"⚠️ Aucun bot trouvé pour <code>{target_tid}</code>.", parse_mode="HTML"); return

    # Recherche par préfixe (on a pu tronquer le nom à 20 chars dans callback_data)
    bot_row = next(
        (b for b in bots if b["project_name"].startswith(pname_req)
                         or b["project_name"] == pname_req),
        None
    )
    if not bot_row:
        names = ", ".join(f"<code>{b['project_name']}</code>" for b in bots)
        await q.message.reply_text(
            f"❌ Projet introuvable.\nDisponibles : {names}", parse_mode="HTML"); return

    buf, fname, caption = _build_project_zip(bot_row, target_tid)
    await q.message.reply_document(
        document=buf, filename=fname, caption=caption, parse_mode="HTML")
    log_activity(tid, "admin_dl", f"{target_tid}/{bot_row['project_name']}")


async def back_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    tid = update.effective_user.id
    context.user_data.clear()
    if is_admin(tid):
        from db import get_bot_assigned_port
        bots      = get_user_bots(tid)
        running_c = sum(1 for b in bots if b["is_running"])
        nb_bots   = sum(1 for b in bots if b.get("project_type", "bot") == "bot")
        nb_web    = sum(1 for b in bots if b.get("project_type", "bot") == "website")
        bot_lines = ""
        for b in bots:
            ico      = "🟢" if b["is_running"] else "🔴"
            ptype    = b.get("project_type", "bot")
            type_ico = "🌐" if ptype == "website" else "🤖"
            port     = get_bot_assigned_port(tid, b["project_name"]) if b["is_running"] else None
            port_str = f" <code>:{port}</code>" if port else ""
            bot_lines += f"\n│  {ico} {type_ico} <b>{b['project_name']}</b>{port_str}"
        msg = (
            "🔑 <b>Panneau Administrateur</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"├ 📦 Projets : {len(bots)}  (🤖 {nb_bots} · 🌐 {nb_web})\n"
            f"│{bot_lines if bot_lines else '  <i>Aucun projet</i>'}\n"
            f"└ 🟢 En ligne : <b>{running_c}/{len(bots)}</b>"
        )
        rows = []
        for b in bots:
            pname      = b["project_name"]
            pname_s    = pname[:16]
            btn_action = (
                InlineKeyboardButton(f"⛔ {pname_s}", callback_data=f"stop:{pname}")
                if b["is_running"] else
                InlineKeyboardButton(f"▶️ {pname_s}", callback_data=f"deploy:{pname}")
            )
            btn_mod = InlineKeyboardButton("✏️", callback_data=f"modify:{pname}")
            btn_dl  = InlineKeyboardButton("📥", callback_data=f"adl:{tid}:{pname}")
            rows.append([btn_action, btn_mod, btn_dl])
        rows += [
            [InlineKeyboardButton("📊 Stats",          callback_data="admin_stats"),
             InlineKeyboardButton("👥 Utilisateurs",   callback_data="admin_users")],
            [InlineKeyboardButton("➕ Nouveau projet",  callback_data="begin_setup")],
            [InlineKeyboardButton("⚙️ Paiements",      callback_data="admin_pay_config"),
             InlineKeyboardButton("📦 ZIP source",     callback_data="admin_source_zip")],
        ]
        await q.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    elif is_subscription_active(tid):
        msg, kb = _blue_panel(tid)
        await q.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)
    else:
        profile = get_user_profile(tid)
        msg, kb = _red_panel(profile or {}, tid)
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════════════
# MODE D'EMPLOI
# ══════════════════════════════════════════════════════════════════════════════

async def guide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le guide complet d'utilisation du Bot Manager."""
    q = update.callback_query
    await q.answer()

    # Récupérer les prix dynamiques depuis la base
    durations = get_durations()
    pro_price = get_pro_price()
    price_lines = "\n".join(
        f"  • {d['label']} → <b>{d['price']} F</b>"
        for d in durations
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 Retour à l'accueil", callback_data="back_home")
    ]])

    # ── Partie 1 : Démarrage, abonnements ─────────────────────────────────────
    part1 = (
        "📖 <b>Mode d'emploi — Bot Manager</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "╔══════════════════════════╗\n"
        "║  🆕  PREMIÈRE UTILISATION  ║\n"
        "╚══════════════════════════╝\n"
        "1️⃣ Tapez /start → cliquez <b>🚀 Héberger un Bot</b>\n"
        "2️⃣ Entrez vos <b>credentials Telegram</b> (une seule fois) :\n"
        "   <code>API_ID</code> → <code>API_HASH</code> → <code>ADMIN_ID</code>\n"
        "   <i>(récupérez-les sur my.telegram.org)</i>\n"
        "3️⃣ Donnez un <b>nom</b> à votre projet\n"
        "4️⃣ Collez le <b>token</b> de votre bot (@BotFather)\n"
        "5️⃣ Envoyez votre <b>fichier ZIP</b> contenant <code>main.py</code>\n"
        "6️⃣ Ajoutez des <b>variables d'environnement</b> si besoin\n"
        "7️⃣ Cliquez <b>✅ Terminer et héberger</b> puis <b>🚀 Héberger</b>\n\n"

        "╔════════════════════════╗\n"
        "║  ➕  AJOUTER UN BOT    ║\n"
        "╚════════════════════════╝\n"
        "Vos credentials sont mémorisés. La prochaine fois :\n"
        "1️⃣ /start → <b>🚀 Héberger un Bot</b>\n"
        "2️⃣ Nom du projet → Token → ZIP → Variables\n"
        "<i>(Pas besoin de re-saisir API_ID / API_HASH / ADMIN_ID)</i>\n\n"

        "╔═══════════════════════════╗\n"
        "║  💳  ABONNEMENTS &amp; PRIX   ║\n"
        "╚═══════════════════════════╝\n"
        f"<b>Plan Standard</b> ({config.MAX_BOTS_BASIC} bots max) :\n"
        f"{price_lines}\n\n"
        f"<b>Plan Pro</b> ({config.MAX_BOTS_PRO} bots max) :\n"
        f"  • 7 jours → <b>{pro_price} F</b>\n\n"
        "🎁 <b>Essai gratuit : 2h offertes</b> à la première connexion.\n\n"
        "Pour payer → /start → <b>💳 Payer un abonnement</b>\n"
        "Envoyez la capture d'écran du paiement, l'admin valide.\n"
    )

    # ── Partie 2 : Gestion bots, commandes, ZIP, dépendances ──────────────────
    part2 = (
        "╔══════════════════════╗\n"
        "║  🤖  GÉRER SES BOTS  ║\n"
        "╚══════════════════════╝\n"
        "• <b>📋 Mes Bots</b> → voir l'état de vos bots\n"
        "• <b>🚀 Démarrer</b> → lancer un bot\n"
        "• <b>⛔ Arrêter</b> → stopper un bot\n"
        "• <b>🗑</b> → supprimer un bot (confirmation requise)\n"
        "• <b>➕ Ajouter un autre bot</b> → déployer un nouveau bot\n\n"

        "╔══════════════════════╗\n"
        "║  ⌨️  COMMANDES        ║\n"
        "╚══════════════════════╝\n"
        "/start → Accueil principal\n"
        "/monbot → Voir mes bots en cours\n"
        "/abonnement → Infos sur mon abonnement\n"
        "/payer → Lancer un paiement\n"
        "/cancel → Annuler une opération en cours\n\n"

        "╔════════════════════════╗\n"
        "║  📦  PRÉPARER SON ZIP  ║\n"
        "╚════════════════════════╝\n"
        "Votre ZIP doit contenir :\n"
        "  • <code>main.py</code> <i>(obligatoire — point d'entrée)</i>\n"
        "  • Autres fichiers <code>.py</code> <i>(optionnel)</i>\n"
        "  • <code>.env</code> <i>(optionnel — variables d'env)</i>\n"
        "<i>Taille max recommandée : 20 Mo</i>\n\n"

        "╔════════════════════════════╗\n"
        "║  🐍  DÉPENDANCES PYTHON    ║\n"
        "╚════════════════════════════╝\n"
        "Le système <b>détecte automatiquement</b> les imports\n"
        "et installe les packages manquants avant démarrage.\n\n"
        "⚡ <b>Pré-installés (instantanément disponibles) :</b>\n"
        "  • <code>telethon</code> — Client Telegram MTProto\n"
        "  • <code>requests</code> / <code>aiohttp</code> — HTTP\n"
        "  • <code>python-dotenv</code> — Chargement .env\n"
        "  • <code>Pillow</code> — Images\n"
        "  • <code>beautifulsoup4</code> / <code>lxml</code> — Scraping\n"
        "  • <code>pycryptodome</code> — Chiffrement\n"
        "  • <code>schedule</code> — Planification\n\n"
        "📦 Tout autre package PyPI est installé automatiquement.\n"
        "⚠️ Non supportés : binaires natifs (numpy avec BLAS, etc.)\n"
    )

    await q.edit_message_text(part1, parse_mode="HTML",
                              disable_web_page_preview=True)
    await q.message.reply_text(part2, parse_mode="HTML",
                               reply_markup=kb, disable_web_page_preview=True)


# ══════════════════════════════════════════════════════════════════════════════
# POST INIT & MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(application: Application) -> None:
    loop = asyncio.get_running_loop()

    def send_message_sync(telegram_id: int, text: str):
        asyncio.run_coroutine_threadsafe(
            application.bot.send_message(chat_id=telegram_id, text=text, parse_mode="Markdown"),
            loop)

    runner_module.set_send_callback(send_message_sync)

    count = runner_module.restart_active_bots()
    logger.info(f"Auto-restart : {count} bot(s) relancé(s).")

    runner_module.start_subscription_checker()

    await application.bot.set_my_commands([
        BotCommand("start",      "Accueil / Tableau de bord"),
        BotCommand("monbot",     "Mes bots et abonnement"),
        BotCommand("abonnement", "Voir mon abonnement"),
        BotCommand("payer",      "Payer / Renouveler"),
        BotCommand("cancel",     "Annuler la configuration"),
    ])


def _start_web_server():
    from web_server import app as flask_app
    flask_app.run(host="0.0.0.0", port=config.PORT, debug=False, use_reloader=False)


def main():
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN manquant.")
    init_db()
    threading.Thread(target=_start_web_server, daemon=True, name="WebServer").start()
    logger.info(f"Dashboard démarré sur le port {config.PORT}")

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    # ConversationHandler unique gérant les deux flux + config admin
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(begin_setup_callback,   pattern="^(begin_setup|add_bot)$"),
            CallbackQueryHandler(admin_pay_config_callback, pattern="^admin_pay_config$"),
            CallbackQueryHandler(modify_bot_callback,    pattern=r"^modify:.+$"),
        ],
        states={
            # Choix du type de déploiement (bot / site web)
            DEPLOY_TYPE_STEP: [
                CallbackQueryHandler(deploy_type_callback, pattern=r"^dep_type:(bot|website)$"),
                CallbackQueryHandler(back_home_callback,   pattern="^back_home$"),
            ],
            # Flux complet — credentials (nouveaux utilisateurs uniquement)
            API_ID_STEP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_id)],
            API_HASH_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_hash)],
            ADMIN_ID_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_admin_id_profile)],
            # Flux commun
            PROJECT_NAME_STEP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_project_name),
                CallbackQueryHandler(confirm_update_callback, pattern=r"^confirm_update:"),
                CallbackQueryHandler(cancel_update_callback,  pattern=r"^cancel_update$"),
            ],
            API_TOKEN_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_token_bot)],
            ZIP_FILE: [
                MessageHandler(filters.Document.ALL, get_zip_file),
                CallbackQueryHandler(back_home_callback, pattern="^back_home$"),
            ],
            # Fenêtre variables d'environnement
            ENV_VAR_NAME: [
                CallbackQueryHandler(env_var_add_callback,      pattern="^envvar_add$"),
                CallbackQueryHandler(env_var_done_callback,     pattern="^envvar_done$"),
                CallbackQueryHandler(env_var_continue_callback, pattern="^envvar_continue$"),
                CallbackQueryHandler(back_home_callback,        pattern="^back_home$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_env_var_name),
            ],
            ENV_VAR_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_env_var_value),
            ],
            # Modification du code d'un projet existant
            MODIFY_ZIP_STEP: [
                MessageHandler(filters.Document.ALL, get_modify_zip_file),
                CallbackQueryHandler(back_home_callback, pattern="^back_home$"),
            ],
            # Config admin paiements
            ADMIN_PAY_MENU: [
                CallbackQueryHandler(admin_pay_menu_cb,
                    pattern="^(admin_pay_edit_info|admin_pay_edit_price|admin_pay_edit_pro|admin_pay_close)$"),
            ],
            ADMIN_PAY_EDIT_INFO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_pay_edit_info)],
            ADMIN_PAY_EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_pay_edit_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
        payment_proof_handler))

    # Commandes publiques
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("monbot",     my_bot_command))
    app.add_handler(CommandHandler("abonnement", abonnement_command))
    app.add_handler(CommandHandler("payer",      payer_command))

    # Commandes admin
    app.add_handler(CommandHandler("activer",      activer_command))
    app.add_handler(CommandHandler("activer_pro",  activer_pro_command))
    app.add_handler(CommandHandler("suspendre",    suspendre_command))
    app.add_handler(CommandHandler("supprimer",    supprimer_command))
    app.add_handler(CommandHandler("stopper",      stopper_command))
    app.add_handler(CommandHandler("temps",        temps_command))
    app.add_handler(CommandHandler("utilisateurs", utilisateurs_command))
    app.add_handler(CommandHandler("dbinfo",       dbinfo_command))
    app.add_handler(CommandHandler("dl",           dl_command))
    app.add_handler(CommandHandler("logs",         logs_command))

    # Callbacks inline
    app.add_handler(CallbackQueryHandler(deploy_callback,         pattern=r"^deploy:.+$"))
    app.add_handler(CallbackQueryHandler(stop_callback,           pattern=r"^stop:.+$"))
    app.add_handler(CallbackQueryHandler(del_ask_callback,        pattern=r"^del_ask:.+$"))
    app.add_handler(CallbackQueryHandler(del_yes_callback,        pattern=r"^del_yes:.+$"))
    app.add_handler(CallbackQueryHandler(del_no_callback,         pattern="^del_no$"))
    app.add_handler(CallbackQueryHandler(my_bots_list_callback,   pattern="^my_bots_list$"))
    # Admin — actions utilisateurs
    app.add_handler(CallbackQueryHandler(admin_del_ask_callback,  pattern=r"^admin_del_ask:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_del_yes_callback,  pattern=r"^admin_del_yes:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_block_callback,    pattern=r"^admin_block:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_unblock_callback,  pattern=r"^admin_unblock:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_stats_callback,    pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_source_zip_callback, pattern="^admin_source_zip$"))
    app.add_handler(CallbackQueryHandler(admin_users_callback,    pattern="^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_dl_callback,       pattern=r"^adl:.+$"))
    app.add_handler(CallbackQueryHandler(payer_info_callback,     pattern="^payer_info$"))
    app.add_handler(CallbackQueryHandler(pay_duration_callback,   pattern=r"^pay_dur:\d+$"))
    app.add_handler(CallbackQueryHandler(pay_pro_callback,        pattern="^pay_pro$"))
    app.add_handler(CallbackQueryHandler(pay_cancel_callback,     pattern="^pay_cancel$"))
    app.add_handler(CallbackQueryHandler(pay_validate_admin_callback, pattern=r"^pay_ok:\d+:\d+:\d$"))
    app.add_handler(CallbackQueryHandler(pay_refuse_admin_callback,  pattern=r"^pay_no:\d+$"))
    app.add_handler(CallbackQueryHandler(back_home_callback,      pattern="^back_home$"))
    app.add_handler(CallbackQueryHandler(guide_callback,          pattern="^guide$"))

    logger.info("Bot Manager démarré...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
