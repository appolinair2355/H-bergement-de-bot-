# Bot Manager — Telegram Multi-Bot Hosting Platform

## Architecture

Application Python permettant d'héberger plusieurs bots Telegram pour des utilisateurs avec un système d'abonnement.

### Fichiers principaux
- `bot.py` — Bot Telegram principal (interface admin + utilisateurs, 1800+ lignes)
- `runner.py` — Gestionnaire de processus des bots utilisateurs (multi-bots en parallèle)
- `db.py` — Couche PostgreSQL (profils, bots, abonnements, paramètres, journal)
- `config.py` — Configuration centralisée (lue depuis variables d'environnement)
- `web_server.py` — Dashboard admin Flask (port 5000)
- `analyzer.py` — Détection des dépendances pip dans le code utilisateur

### Stack
- **Python 3.12** (Replit) / 3.11.9 (original)
- **python-telegram-bot 21.3** — Framework Telegram async
- **PostgreSQL** via `psycopg2-binary` avec pool de connexions
- **Flask** — Dashboard d'administration web

## Configuration (Variables d'environnement / Secrets)

| Variable | Description | Obligatoire |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token du bot manager (@BotFather) | ✅ |
| `DATABASE_URL` | URL PostgreSQL Replit (auto-détectée) | ✅ |
| `RENDER_DATABASE_URL` | Alternative à DATABASE_URL (Render.com) | — |
| `ADMIN_TELEGRAM_IDS` | IDs Telegram des admins (CSV) | Optionnel |
| `PORT` | Port du serveur Flask (défaut: 5000) | Optionnel |
| `DASHBOARD_SECRET` | Token d'accès au dashboard web | Optionnel |

**Admin hardcodé :** `8649780855` (dans `config.py`)

## Fonctionnalités

### Utilisateurs
- Essai gratuit 2h à l'inscription
- Upload de bot via ZIP (main.py, bot.py, app.py... détection automatique)
- Jusqu'à 5 bots (plan standard) ou 10 bots (plan Pro)
- Affichage du port de chaque bot en cours d'exécution
- Variables d'environnement personnalisées par bot

### Admin
- Bypass complet du système d'abonnement
- Hébergement illimité de bots
- Panel utilisateurs avec statuts d'abonnement
- Validation des paiements via screenshot
- Configuration des prix et tarifs
- Téléchargement des ZIPs des projets
- Journal d'activité

### Hosting des bots utilisateurs
- Chaque bot reçoit un port fixe dédié (plage 11000-13000)
- Port persiste en DB et réutilisé au redémarrage
- Auto-restart des bots après redémarrage du manager
- Monitoring avec détection des packages manquants
- Checker toutes les 10 min pour les abonnements expirés (exclut les admins)

## Dashboard Web

Accessible sur `/?token=<DASHBOARD_SECRET>` (défaut: `botmanager_admin_2024`)
- Vue globale des utilisateurs
- Statuts d'abonnement en temps réel
- Téléchargement des ZIPs

## Workflow Replit

- **Start application** → `python3 bot.py` sur port 5000 (webview)
- Le bot Telegram et le serveur Flask démarrent ensemble (Flask dans un thread daemon)

## Corrections apportées (2026-03-30)

1. **Admin bypass** : Les admins ne voient plus le formulaire de paiement, sautent directement au nom du projet
2. **Crash "Terminer"** : Nettoyage du `user_data` entre sessions, `.get()` sécurisé, auto-déploiement direct
3. **Détection fichier principal** : Accepte main.py, bot.py, app.py, index.py, run.py, start.py ou tout .py
4. **Port affiché** : Visible dans les panneaux admin et utilisateur pour chaque bot en ligne
5. **Navigation** : Bouton "Retour" dans tous les panneaux, `back_home_callback` intelligent (admin/user)
6. **Checker d'expiration** : N'arrête plus les bots des admins
7. **Chemin de suppression** : Corrigé pour pointer vers `user_bots/` au lieu de la racine
