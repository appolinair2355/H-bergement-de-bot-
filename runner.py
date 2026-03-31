"""
runner.py — Gestionnaire de processus bots utilisateurs (multi-bots)
"""
import os
import subprocess
import signal
import logging
import threading
import time
import re
import sys
from pathlib import Path

from db import (
    get_bot, get_project, set_running, set_bot_running,
    get_all_bots, get_expired_running_bots,
    get_bot_assigned_port, set_bot_assigned_port, get_all_assigned_ports,
)

logger = logging.getLogger(__name__)

USER_BOTS_DIR = Path(__file__).parent / "user_bots"
USER_BOTS_DIR.mkdir(exist_ok=True)

_send_message_callback = None

# Port réservé par le dashboard (ne jamais le donner à un bot utilisateur)
_DASHBOARD_PORT = int(os.environ.get("PORT", 5000))
# Plage de ports attribués aux bots utilisateurs
# Chaque utilisateur dispose d'un bloc de 20 ports
_BOT_PORT_START = 11000
_BOT_PORT_END   = 13000   # 2000 ports = 100 utilisateurs × 20 bots max

_port_lock = threading.Lock()


def _is_port_free(port: int) -> bool:
    """Vérifie si un port est libre sur la machine."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("", port))
            return True
        except OSError:
            return False


def _find_free_port(exclude: set = None) -> int:
    """Trouve un port libre dans la plage, en excluant les ports déjà assignés."""
    if exclude is None:
        exclude = set()
    exclude.add(_DASHBOARD_PORT)
    for port in range(_BOT_PORT_START, _BOT_PORT_END):
        if port in exclude:
            continue
        if _is_port_free(port):
            return port
    # Fallback OS
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _get_dedicated_port(telegram_id: int, project_name: str) -> int:
    """
    Retourne le port FIXE assigné à ce bot.
    - Si déjà assigné en DB et libre → on le réutilise.
    - Si déjà assigné mais occupé (redémarrage rapide) → on attend puis réessaie.
    - Si jamais assigné → on choisit le prochain port libre
      en évitant tous les ports déjà pris par d'autres bots.
    Le port est ensuite persisté en DB pour ce bot.
    """
    with _port_lock:
        stored = get_bot_assigned_port(telegram_id, project_name)

        if stored is not None:
            # Attendre jusqu'à 5 s que le port se libère (arrêt du process précédent)
            for _ in range(10):
                if _is_port_free(stored):
                    logger.info(f"Port réutilisé pour {telegram_id}/{project_name}: {stored}")
                    return stored
                time.sleep(0.5)
            # Le port est toujours occupé → trouver un nouveau
            logger.warning(f"Port {stored} occupé pour {telegram_id}/{project_name}, réassignation")

        # Trouver un port libre en évitant tous les ports déjà assignés en DB
        taken = get_all_assigned_ports()
        taken.add(_DASHBOARD_PORT)
        port = _find_free_port(exclude=taken)
        set_bot_assigned_port(telegram_id, project_name, port)
        logger.info(f"Nouveau port assigné à {telegram_id}/{project_name}: {port}")
        return port


def set_send_callback(fn):
    global _send_message_callback
    _send_message_callback = fn


def _send_to_user(telegram_id: int, text: str):
    if _send_message_callback:
        try:
            _send_message_callback(telegram_id, text)
        except Exception as e:
            logger.error(f"Failed to send message to {telegram_id}: {e}")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().strip())[:24]


def get_user_bot_path(telegram_id: int, project_name: str = "Bot Principal") -> Path:
    return USER_BOTS_DIR / f"bot_{telegram_id}_{_slug(project_name)}.py"


def _extract_missing_packages(stderr_text: str) -> list[str]:
    packages = []
    for m in re.findall(r"No module named '([^']+)'", stderr_text):
        pkg = m.split(".")[0]
        if pkg not in packages:
            packages.append(pkg)
    return packages


def _install_packages(packages: list[str], telegram_id: int) -> bool:
    if not packages:
        return True
    _send_to_user(telegram_id,
        f"📦 *Installation des dépendances...*\n`{', '.join(packages)}`")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + packages,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            _send_to_user(telegram_id, f"✅ Installé : `{', '.join(packages)}`")
            return True
        _send_to_user(telegram_id, f"❌ Échec installation :\n```{result.stderr[-400:]}```")
        return False
    except subprocess.TimeoutExpired:
        _send_to_user(telegram_id, "❌ Timeout installation des packages.")
        return False
    except Exception as e:
        _send_to_user(telegram_id, f"❌ Erreur : {e}")
        return False


def _monitor_process(proc: subprocess.Popen, telegram_id: int,
                      project_name: str, api_token: str):
    stderr_output, stdout_output = [], []

    def read_stderr():
        for line in proc.stderr:
            stderr_output.append(line.decode("utf-8", errors="replace").rstrip())

    def read_stdout():
        for line in proc.stdout:
            stdout_output.append(line.decode("utf-8", errors="replace").rstrip())

    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_out = threading.Thread(target=read_stdout, daemon=True)
    t_err.start(); t_out.start()
    time.sleep(8)

    if proc.poll() is not None:
        set_bot_running(telegram_id, project_name, False, None)
        t_err.join(timeout=2); t_out.join(timeout=2)
        all_output   = "\n".join(stderr_output + stdout_output).strip()
        missing_pkgs = _extract_missing_packages(all_output)

        if missing_pkgs:
            installed = _install_packages(missing_pkgs, telegram_id)
            if installed:
                _send_to_user(telegram_id, "🔄 *Redémarrage de votre bot...*")
                success, message = start_user_bot(telegram_id, project_name)
                _send_to_user(telegram_id, message)
                return

        err_preview = (all_output[-1000:] if all_output
                       else "Aucun détail. Vérifiez votre `.env`.")
        _send_to_user(telegram_id,
            f"❌ *Bot « {project_name} » planté (code {proc.returncode})*\n\n"
            f"```\n{err_preview}\n```\n\n"
            "Reconfigurez via /start → Hébergement.")
    else:
        logger.info(f"Bot {telegram_id}/{project_name} (PID {proc.pid}) running ✓")
        _send_welcome_via_user_bot(api_token, telegram_id, project_name)


def _send_welcome_via_user_bot(api_token: str, owner_tid: int, project_name: str):
    import urllib.request, urllib.parse
    try:
        text = (
            f"✅ *Bot « {project_name} » en ligne !*\n\n"
            "Démarré avec succès — il est actif et prêt.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🏆 *Bienvenue sur la plateforme d'hébergement de*\n"
            "*Sossou Kouamé Appolinaire* 🏆\n\n"
            "📞 Pour plus d'informations, contactez-moi sur :\n"
            "• *Telegram* : @2290195501564\n"
            "• *WhatsApp* : +229 01 95 50 15 64\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        params = urllib.parse.urlencode(
            {"chat_id": owner_tid, "text": text, "parse_mode": "Markdown"})
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{api_token}/sendMessage?{params}", timeout=10)
    except Exception as e:
        logger.warning(f"Welcome via user bot failed: {e}")


def start_user_bot(telegram_id: int, project_name: str = None) -> tuple[bool, str]:
    import json as _json

    if project_name is None:
        from db import get_user_bots
        bots = get_user_bots(telegram_id)
        if not bots:
            return False, "❌ Aucun bot trouvé pour cet utilisateur."
        project_name = bots[0]["project_name"]

    project = get_bot(telegram_id, project_name)
    if not project:
        return False, f"❌ Bot « {project_name} » introuvable."

    if project["is_running"] and project["pid"]:
        try:
            os.kill(project["pid"], 0)
            return False, f"⚠️ Le bot « {project_name} » est déjà actif."
        except ProcessLookupError:
            set_bot_running(telegram_id, project_name, False, None)

    api_token = project["api_token"]
    main_code = project["main_py"]
    bot_file  = get_user_bot_path(telegram_id, project_name)

    bot_file.write_text(inject_token(main_code, api_token), encoding="utf-8")

    # Extra files
    extra = project.get("extra_files") or {}
    if isinstance(extra, str):
        extra = _json.loads(extra)
    for fname, content in extra.items():
        (USER_BOTS_DIR / fname).write_text(inject_token(content, api_token), encoding="utf-8")

    # Env vars
    env_vars = project.get("env_vars") or {}
    if isinstance(env_vars, str):
        env_vars = _json.loads(env_vars)

    # Pré-install packages
    try:
        from analyzer import detect_local_dependencies
        _, pip_deps = detect_local_dependencies(main_code)
        if pip_deps:
            _install_packages(pip_deps, telegram_id)
    except Exception as e:
        logger.warning(f"Pre-install check failed: {e}")

    env = os.environ.copy()
    # Port fixe dédié à ce bot (persisté en DB, jamais partagé avec un autre bot)
    dedicated_port = _get_dedicated_port(telegram_id, project_name)
    env["PORT"] = str(dedicated_port)
    logger.info(f"Port attribué au bot {telegram_id}/{project_name}: {dedicated_port}")
    env.update({"TOKEN": api_token, "BOT_TOKEN": api_token,
                 "TELEGRAM_TOKEN": api_token, "API_TOKEN": api_token})
    for k, v in env_vars.items():
        env[k] = str(v)
    env["PYTHONPATH"] = str(USER_BOTS_DIR) + ":" + env.get("PYTHONPATH", "")

    try:
        proc = subprocess.Popen(
            ["python3", str(bot_file)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        set_bot_running(telegram_id, project_name, True, proc.pid)
        logger.info(f"Started {telegram_id}/{project_name} PID={proc.pid} PORT={dedicated_port}")
        threading.Thread(
            target=_monitor_process,
            args=(proc, telegram_id, project_name, api_token),
            daemon=True,
        ).start()
        return True, (
            f"✅ *Bot « {project_name} » démarré !*\n\n"
            f"├ PID : `{proc.pid}`\n"
            f"└ Port : `{dedicated_port}`\n\n"
            "Démarrage en cours — vous recevrez un message de confirmation dans quelques secondes."
        )
    except Exception as e:
        logger.error(f"Start error {telegram_id}/{project_name}: {e}")
        return False, f"❌ Erreur démarrage : {e}"


def stop_user_bot(telegram_id: int, project_name: str = None) -> tuple[bool, str]:
    if project_name is None:
        from db import get_user_bots
        bots = get_user_bots(telegram_id)
        if not bots:
            return False, "⚠️ Aucun bot trouvé."
        project_name = bots[0]["project_name"]

    project = get_bot(telegram_id, project_name)
    if not project or not project["is_running"]:
        return False, "⚠️ Aucun bot actif à arrêter."

    pid = project["pid"]
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    set_bot_running(telegram_id, project_name, False, None)
    logger.info(f"Stopped {telegram_id}/{project_name} PID={pid}")
    return True, f"✅ Bot « {project_name} » arrêté."


def inject_token(code: str, token: str) -> str:
    """
    Injecte le token Telegram dans le code utilisateur.
    Remplace uniquement les variables clairement liées à Telegram
    (BOT_TOKEN, TELEGRAM_TOKEN, TOKEN — mais PAS API_TOKEN qui peut
    désigner un token tiers comme OpenAI, CoinGecko, etc.).
    """
    # Variables d'affectation directe (ex : BOT_TOKEN = "...")
    # N.B. : API_TOKEN volontairement exclu — trop générique
    var_patterns = [
        r'((?:TELEGRAM_BOT_TOKEN|TELEGRAM_TOKEN|telegram_token|BOT_TOKEN|bot_token)\s*=\s*)["\'][^"\']*["\']',
        # TOKEN = "..." seulement si c'est seul (pas MYAPP_TOKEN, STRIPE_TOKEN, etc.)
        r'(?<![A-Z_])(TOKEN\s*=\s*)["\'][^"\']*["\']',
        # os.environ.get("BOT_TOKEN", ...) / os.environ.get("TOKEN", ...)
        r'(os\.environ\.get\(["\'](?:TELEGRAM_BOT_TOKEN|TELEGRAM_TOKEN|BOT_TOKEN|TOKEN)["\'],\s*)["\'][^"\']*["\']',
    ]
    # Patterns dans les appels de bibliothèques Telegram
    inline_patterns = [
        r'(Bot\s*\(\s*token\s*=\s*)["\'][^"\']*["\']',
        r'(TelegramClient\s*\([^)]*bot_token\s*=\s*)["\'][^"\']*["\']',
        r'(Updater\s*\(["\'])[^"\']*(["\'])',
        r'(ApplicationBuilder\s*\(\s*\)\s*\.token\s*\(\s*)["\'][^"\']*["\'](\s*\))',
    ]
    modified, found = code, False
    for p in var_patterns:
        if re.search(p, modified):
            modified = re.sub(p, lambda m: m.group(1) + f'"{token}"', modified)
            found = True
    for p in inline_patterns:
        if re.search(p, modified):
            def _r(m, _t=token):
                g = m.groups()
                return (g[0] + f'"{_t}"' + g[1]) if len(g) == 2 else (g[0] + f'"{_t}"')
            modified = re.sub(p, _r, modified)
            found = True
    # Aucun pattern trouvé → injecter des variables en haut du fichier
    # On n'inclut PAS API_TOKEN pour éviter d'écraser d'autres APIs
    if not found:
        modified = (
            f'TOKEN="{token}"\n'
            f'BOT_TOKEN="{token}"\n'
            f'TELEGRAM_TOKEN="{token}"\n'
            f'TELEGRAM_BOT_TOKEN="{token}"\n\n'
        ) + code
    return modified


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-RESTART AU DÉMARRAGE
# ══════════════════════════════════════════════════════════════════════════════

def restart_active_bots() -> int:
    """Relance tous les bots marqués is_running=True après un redémarrage du manager."""
    import config as _cfg
    from db import get_all_bots, is_subscription_active, log_activity
    count = 0
    bots  = get_all_bots()
    # Seulement ceux marqués "en cours" avant l'arrêt
    running = [b for b in bots if b["is_running"]]
    logger.info(f"Auto-restart : {len(running)} bot(s) à relancer.")
    for b in running:
        tid   = b["telegram_id"]
        pname = b["project_name"]
        # Les admins ignorent la vérification d'abonnement
        authorized = (tid in _cfg.ADMIN_TELEGRAM_IDS) or is_subscription_active(tid)
        if authorized:
            ok, msg = start_user_bot(tid, pname)
            if ok:
                count += 1
                log_activity(tid, "auto_restart", pname)
                logger.info(f"Auto-restart OK → {tid}/{pname}")
            else:
                set_bot_running(tid, pname, False, None)
                logger.warning(f"Auto-restart FAILED → {tid}/{pname} : {msg}")
                _send_to_user(tid,
                    f"⚠️ Bot « {pname} » n'a pas pu être relancé.\n"
                    "Tapez /monbot pour le redémarrer manuellement.")
        else:
            set_bot_running(tid, pname, False, None)
            logger.info(f"Auto-restart SKIP (abonnement expiré) → {tid}/{pname}")
    logger.info(f"Auto-restart terminé : {count}/{len(running)} bot(s) relancé(s).")
    return count


# ══════════════════════════════════════════════════════════════════════════════
# VÉRIFICATEUR D'ABONNEMENTS (toutes les 10 min)
# ══════════════════════════════════════════════════════════════════════════════

def _subscription_checker_loop():
    import config as _cfg2
    logger.info("Subscription checker started.")
    while True:
        try:
            for b in get_expired_running_bots(admin_ids=_cfg2.ADMIN_TELEGRAM_IDS):
                tid   = b["telegram_id"]
                pname = b["project_name"]
                pid   = b.get("pid")
                logger.info(f"Subscription expired: stopping {tid}/{pname} PID={pid}")
                if pid:
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.kill(pid, sig)
                            time.sleep(2)
                        except ProcessLookupError:
                            break
                set_bot_running(tid, pname, False, None)
                _send_to_user(tid,
                    f"⛔ *Bot « {pname} » suspendu — abonnement expiré*\n\n"
                    "Tapez /payer pour renouveler.")
        except Exception as e:
            logger.error(f"Subscription checker error: {e}")
        time.sleep(600)


def start_subscription_checker():
    t = threading.Thread(target=_subscription_checker_loop, daemon=True,
                          name="SubscriptionChecker")
    t.start()
    logger.info("Subscription checker thread launched.")
