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

import config
from db import (
    init_db,
    # Profils
    get_user_profile, upsert_user_profile, give_free_trial,
    is_subscription_active, is_pro_active,
    set_subscription, set_subscription_days, set_pro_subscription, revoke_subscription,
    get_all_profiles, delete_user,
    # Bots
    save_bot, get_bot, get_user_bots, count_user_bots,
    delete_bot,
    set_bot_running, set_all_bots_stopped,
    # Settings
    get_setting, set_setting, get_durations, get_pro_price,
    # Compat
    get_project,
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
 ADMIN_PAY_MENU, ADMIN_PAY_EDIT_INFO, ADMIN_PAY_EDIT_PRICE) = range(13)

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
            [InlineKeyboardButton("👥 Mes Utilisateurs",       callback_data="admin_users")],
            [InlineKeyboardButton("🚀 Hébergement",             callback_data="begin_setup")],
            [InlineKeyboardButton("⚙️ Configuration paiements", callback_data="admin_pay_config")],
            [InlineKeyboardButton("📖 Mode d'emploi",           callback_data="guide")],
        ])
    profile = get_user_profile(tid)
    bots    = get_user_bots(tid) if profile else []
    rows    = []
    if bots:
        rows.append([InlineKeyboardButton("📋 Mes Bots", callback_data="my_bots_list")])
    rows += [
        [InlineKeyboardButton("🚀 Héberger un Bot",       callback_data="begin_setup")],
        [InlineKeyboardButton("💳 Payer un abonnement",   callback_data="payer_info")],
        [InlineKeyboardButton("📖 Mode d'emploi",         callback_data="guide")],
    ]
    return InlineKeyboardMarkup(rows)

def _red_panel(profile: dict) -> tuple[str, InlineKeyboardMarkup]:
    sub_end = profile.get("subscription_end") if profile else None
    if sub_end:
        msg = (f"🔴 *Abonnement expiré*\n\n"
               f"Expiré le : {_sub_expire_str(sub_end)}\n\n"
               "Renouvelez pour réactiver votre hébergement.")
    else:
        msg = ("🔴 *Aucun abonnement actif*\n\n"
               "Choisissez une durée pour activer l'hébergement de votre bot.")
    kb = [[InlineKeyboardButton("💳 Payer mon abonnement", callback_data="payer_info")]]
    return msg, InlineKeyboardMarkup(kb)

def _blue_panel(tid: int) -> tuple[str, InlineKeyboardMarkup]:
    profile = get_user_profile(tid) or {}
    sub_end = profile.get("subscription_end")
    pro_end = profile.get("pro_subscription_end")
    bots    = get_user_bots(tid)

    remaining = _sub_remaining_str(sub_end) if sub_end else "—"
    exp_str   = _sub_expire_str(sub_end)    if sub_end else "—"
    running_c = sum(1 for b in bots if b["is_running"])

    pro_line = ""
    if is_pro_active(tid) and pro_end:
        pro_line = f"\n├ ⭐ Pro : {_sub_remaining_str(pro_end)} restants"

    bots_lines = ""
    for b in bots:
        ico = "🟢" if b["is_running"] else "🔴"
        bots_lines += f"\n│   {ico} {b['project_name']}"

    msg = (
        "🔵 *Tableau de bord — Abonnement actif*\n\n"
        f"├ ✅ Abonnement : *Actif*\n"
        f"├ ⏳ Temps restant : *{remaining}*\n"
        f"├ 📅 Expire le : {exp_str}"
        f"{pro_line}\n"
        f"├ 🤖 Bots ({len(bots)}/{_bot_limit(tid)}) :{bots_lines if bots_lines else ' aucun'}\n"
        f"└ 🟢 En ligne : {running_c}/{len(bots)}"
    )
    rows = []
    for b in bots:
        pname = b["project_name"]
        if b["is_running"]:
            btn1 = InlineKeyboardButton(f"⛔ Arrêter {pname}", callback_data=f"stop:{pname}")
        else:
            btn1 = InlineKeyboardButton(f"🚀 Démarrer {pname}", callback_data=f"deploy:{pname}")
        btn2 = InlineKeyboardButton("🗑", callback_data=f"del_ask:{pname}")
        rows.append([btn1, btn2])
    rows.append([InlineKeyboardButton("➕ Ajouter un autre bot", callback_data="add_bot")])
    return msg, InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDE /start — Accueil intelligent
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    tid     = update.effective_user.id
    profile = get_user_profile(tid)

    # Essai gratuit 2h : première visite
    if not profile:
        give_free_trial(tid)
        profile = get_user_profile(tid)
        trial_notice = (
            f"\n\n🎁 *Essai gratuit activé : {config.FREE_TRIAL_HOURS}h offerts !*\n"
            "_Utilisez ce temps pour configurer et tester votre bot._"
        )
    else:
        trial_notice = ""

    kb = _welcome_keyboard(tid)

    if profile and (profile.get("nom") or "").strip():
        # Utilisateur connu
        active = is_subscription_active(tid)
        if active:
            msg, kb2 = _blue_panel(tid)
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb2)
        else:
            msg, kb2 = _red_panel(profile)
            await update.message.reply_text(
                f"👋 Bon retour *{profile['prenom']} {profile['nom']}* !\n\n"
                + msg, parse_mode="Markdown", reply_markup=kb2)
    else:
        await update.message.reply_text(
            f"👋 *Bienvenue sur le Bot Manager !*\n\n"
            "Je vous aide à héberger vos bots Telegram en quelques étapes."
            f"{trial_notice}\n\n"
            "Choisissez une option :",
            parse_mode="Markdown",
            reply_markup=kb,
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

    # Enregistrer le nom Telegram automatiquement (sans le demander)
    tg_nom    = (user.last_name  or "").strip()
    tg_prenom = (user.first_name or "").strip()
    if not profile:
        give_free_trial(tid)
    upsert_user_profile(tid, nom=tg_nom, prenom=tg_prenom)

    # Flux court UNIQUEMENT si API_ID déjà enregistré
    has_credentials = bool(
        profile and
        (profile.get("profile_env_vars") or {}).get("API_ID")
    )

    if has_credentials:
        # Utilisateur connu → flux court : nom projet → token → ZIP
        context.user_data["is_returning"]    = True
        context.user_data["profile_env_vars"] = dict(profile.get("profile_env_vars") or {})
        await q.edit_message_text(
            "📋 *Nom de votre projet / bot ?*\n\n"
            "_Ex : Mon Scraper, Bot Shop, Assistant..._\n"
            "_(Max 30 caractères)_",
            parse_mode="Markdown")
        return PROJECT_NAME_STEP
    else:
        # Nouveau utilisateur → flux complet : API_ID → API_HASH → ADMIN_ID → projet → token → ZIP
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

    # Vérifier si ce nom de projet existe déjà
    existing = get_bot(tid, name)
    if existing:
        await update.message.reply_text(
            f"⚠️ Un bot nommé « *{name}* » existe déjà.\n\n"
            "Continuer *mettra à jour* ce bot (nouveau ZIP + token).\n"
            "Ou tapez un autre nom.",
            parse_mode="Markdown")

    context.user_data["project_name"] = name
    await update.message.reply_text(
        f"✅ Nom du projet : *{name}*\n\n"
        "🔑 *Token API du bot ?*\n"
        "_Obtenez un token via @BotFather sur Telegram._",
        parse_mode="Markdown")
    return API_TOKEN_STEP

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

async def get_zip_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Envoyez un fichier `.zip`.", parse_mode="Markdown")
        return ZIP_FILE
    if not (doc.file_name or "").lower().endswith(".zip"):
        await update.message.reply_text(f"❌ `{doc.file_name}` n'est pas un ZIP.", parse_mode="Markdown")
        return ZIP_FILE

    wait = await update.message.reply_text("⬇️ *Téléchargement...*", parse_mode="Markdown")
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

    py_files: dict[str, str] = {}
    env_vars: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.endswith("/") or name.startswith("__MACOSX"):
                    continue
                basename = os.path.basename(name)
                if not basename:
                    continue
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
    except zipfile.BadZipFile:
        await wait.edit_text("❌ ZIP invalide.")
        return ZIP_FILE

    if "main.py" not in py_files:
        await wait.edit_text(
            "❌ Aucun `main.py` dans le ZIP.\n\nFichiers trouvés :\n"
            + "\n".join(f"• `{f}`" for f in sorted(py_files)),
            parse_mode="Markdown")
        return ZIP_FILE

    main_code   = py_files.pop("main.py")
    extra_files = py_files
    _, pip_deps = detect_local_dependencies(main_code)

    all_names = ["main.py"] + sorted(extra_files)
    env_note  = " + `.env` ✅" if env_vars else ""
    pkg_str   = "  " + "\n  ".join(f"✅ `{p}`" for p in pip_deps) if pip_deps else "  Aucun détecté"
    await wait.edit_text(
        f"📋 *Analyse :*\n\n"
        f"━━ *Fichiers ({len(all_names)}) :*{env_note} ━━\n"
        + "  " + "\n  ".join(f"📄 `{f}`" for f in all_names)
        + f"\n\n━━ *Packages :* ━━\n{pkg_str}",
        parse_mode="Markdown")

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
    ])

    text = header + body + footer
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=kb)
    return ENV_VAR_NAME


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
    project_name = context.user_data["project_name"]
    api_token    = context.user_data["api_token"]
    profile      = get_user_profile(tid) or {}
    nom          = profile.get("nom", "")
    prenom       = profile.get("prenom", "")
    msg          = update.effective_message   # fonctionne pour message ET callback

    # Vérification limite de bots
    limit    = _bot_limit(tid)
    nb_bots  = count_user_bots(tid)
    existing = get_bot(tid, project_name)
    if not existing and nb_bots >= limit:
        if not is_pro_active(tid) and limit == config.MAX_BOTS_BASIC:
            pro_price = get_pro_price()
            kb = [[InlineKeyboardButton(
                f"⭐ Passer Pro ({config.MAX_BOTS_PRO} bots) — {pro_price} F/sem",
                callback_data="pay_pro")]]
            await msg.reply_text(
                f"⛔ <b>Limite atteinte : {config.MAX_BOTS_BASIC} bots</b>\n\n"
                "Vous avez atteint la limite du plan standard.\n"
                f"Passez au plan <b>Pro</b> pour héberger jusqu'à {config.MAX_BOTS_PRO} bots.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb))
            return ConversationHandler.END
        elif limit == config.MAX_BOTS_PRO:
            await msg.reply_text(
                f"⛔ <b>Limite Pro atteinte : {config.MAX_BOTS_PRO} bots.</b>\n\nContactez l'admin pour augmenter votre limite.",
                parse_mode="HTML")
            return ConversationHandler.END

    row = save_bot(tid, project_name, api_token, main_code,
                   extra_files, env_vars, nom, prenom)
    pnum     = row["project_number"]
    date_str = row["date_creation"].strftime("%d/%m/%Y à %H:%M")

    extra_info = ""
    if extra_files:
        extra_info = "├ 📎 Fichiers : " + ", ".join(f"<code>{f}</code>" for f in extra_files) + "\n"

    summary = (
        f"✅ <b>Bot « {project_name} » enregistré !</b>\n\n"
        f"├ 👤 {nom} {prenom}\n"
        f"├ 📁 Projet N°{pnum}\n"
        f"├ 📅 {date_str}\n"
        f"├ 🤖 Token : <code>{api_token[:15]}...</code>\n"
        f"{extra_info}\n"
        "Appuyez sur <b>Héberger</b> pour lancer votre bot."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Héberger", callback_data=f"deploy:{project_name}")],
        [InlineKeyboardButton("➕ Ajouter un autre bot", callback_data="add_bot")],
    ])
    await msg.reply_text(summary, parse_mode="HTML", reply_markup=kb)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Configuration annulée. Tapez /start.")
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
    success, message = start_user_bot(tid, pname)
    if success:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⛔ Arrêter {pname}", callback_data=f"stop:{pname}")],
            [InlineKeyboardButton("📋 Mes bots", callback_data="my_bots_list")],
        ])
    else:
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
    # 2. Supprimer le fichier .py sur disque
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in pname.lower())
    bot_file = os.path.join(
        os.path.dirname(__file__),
        f"bot_{tid}_{safe}.py"
    )
    if os.path.exists(bot_file):
        os.remove(bot_file)
    # 3. Supprimer le ZIP sauvegardé
    zip_file = os.path.join(
        os.path.dirname(__file__), "uploads",
        f"{tid}_{safe}.zip"
    )
    if os.path.exists(zip_file):
        os.remove(zip_file)
    # 4. Supprimer de la base de données
    delete_bot(tid, pname)
    # 5. Retour au panel
    msg = f"🗑 *Bot « {pname} » supprimé.*\n\n"
    bots = get_user_bots(tid)
    if bots:
        msg2, kb2 = _blue_panel(tid)
        await q.edit_message_text(msg + msg2, parse_mode="Markdown", reply_markup=kb2)
    else:
        kb2 = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Ajouter un bot", callback_data="begin_setup")
        ]])
        await q.edit_message_text(
            msg + "_Vous n'avez plus de bots hébergés._",
            parse_mode="Markdown", reply_markup=kb2)

async def del_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annule la suppression, retourne au panel."""
    q = update.callback_query
    await q.answer("Suppression annulée.")
    tid = update.effective_user.id
    msg, kb = _blue_panel(tid)
    await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)

async def my_bots_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    tid = update.effective_user.id
    if is_subscription_active(tid) or is_admin(tid):
        msg, kb = _blue_panel(tid)
    else:
        profile = get_user_profile(tid)
        msg, kb = _red_panel(profile or {})
    await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)


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
    else:
        sub_end = set_subscription(user_tid, hours)
        label   = f"{hours}h"

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
        await q.answer("❌", show_alert=True); return
    profiles = get_all_profiles()
    if not profiles:
        await q.edit_message_text("Aucun utilisateur.")
        return
    lines = [f"👥 *Utilisateurs ({len(profiles)}) :*\n"]
    for p in profiles:
        tid    = p["telegram_id"]
        active = is_subscription_active(tid)
        pro    = is_pro_active(tid)
        nb     = count_user_bots(tid)
        icons  = ("✅" if active else "⛔") + (" ⭐" if pro else "")
        sub_end = p.get("subscription_end")
        rem = _sub_remaining_str(sub_end) if active and sub_end else ("expiré" if sub_end else "aucun")
        lines.append(f"{icons} *{p.get('prenom','')} {p.get('nom','')}*\n"
                     f"   `{tid}` — {nb} bots — _{rem}_")
    await q.edit_message_text("\n\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Retour", callback_data="back_home")]]))


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
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

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

async def back_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🏠 Tapez /start pour revenir à l'accueil.")


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

    app = Application.builder().token(token).post_init(post_init).build()

    # ConversationHandler unique gérant les deux flux + config admin
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(begin_setup_callback, pattern="^(begin_setup|add_bot)$"),
            CallbackQueryHandler(admin_pay_config_callback, pattern="^admin_pay_config$"),
        ],
        states={
            # Flux complet — credentials (nouveaux utilisateurs uniquement)
            API_ID_STEP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_id)],
            API_HASH_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_hash)],
            ADMIN_ID_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_admin_id_profile)],
            # Flux commun
            PROJECT_NAME_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_project_name)],
            API_TOKEN_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_token_bot)],
            ZIP_FILE:       [MessageHandler(filters.Document.ALL, get_zip_file)],
            # Fenêtre variables d'environnement
            ENV_VAR_NAME: [
                CallbackQueryHandler(env_var_add_callback,  pattern="^envvar_add$"),
                CallbackQueryHandler(env_var_done_callback, pattern="^envvar_done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_env_var_name),
            ],
            ENV_VAR_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_env_var_value),
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

    # Callbacks inline
    app.add_handler(CallbackQueryHandler(deploy_callback,         pattern=r"^deploy:.+$"))
    app.add_handler(CallbackQueryHandler(stop_callback,           pattern=r"^stop:.+$"))
    app.add_handler(CallbackQueryHandler(del_ask_callback,        pattern=r"^del_ask:.+$"))
    app.add_handler(CallbackQueryHandler(del_yes_callback,        pattern=r"^del_yes:.+$"))
    app.add_handler(CallbackQueryHandler(del_no_callback,         pattern="^del_no$"))
    app.add_handler(CallbackQueryHandler(my_bots_list_callback,   pattern="^my_bots_list$"))
    app.add_handler(CallbackQueryHandler(admin_users_callback,    pattern="^admin_users$"))
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
