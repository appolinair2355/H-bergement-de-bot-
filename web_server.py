"""
web_server.py — Panneau d'administration Bot Manager
Dashboard : https://<app>.replit.dev/?token=<DASHBOARD_SECRET>
Sites web : https://<app>.replit.dev/site/<telegram_id>/<slug>/
"""
import json
import os
import re
import time
import requests as _requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string, send_file, abort, Response, stream_with_context

import config
from db import (get_all_projects, get_project, set_subscription, revoke_subscription,
                is_subscription_active, get_bot, get_bot_assigned_port, get_user_bots)
from runner import stop_user_bot

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")

app = Flask(__name__)


# ── Auth helper ──────────────────────────────────────────────────────────────
def _auth(req) -> bool:
    return req.args.get("token") == config.DASHBOARD_SECRET


def _sub_info(project: dict) -> dict:
    tid = project["telegram_id"]
    active = is_subscription_active(tid)
    sub_end = project.get("subscription_end")
    if not sub_end:
        return {"label": "Aucun", "badge": "badge-none", "days": -1, "hours": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(sub_end, "tzinfo") and sub_end.tzinfo is not None:
        sub_end = sub_end.replace(tzinfo=None)
    if active:
        remaining = sub_end - now
        days  = remaining.days
        hours = remaining.seconds // 3600
        return {"label": f"{days}j {hours}h", "badge": "badge-active", "days": days, "hours": hours}
    else:
        return {"label": "Expiré", "badge": "badge-expired", "days": 0, "hours": 0}


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Manager — Admin</title>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3e;
    --text: #e2e8f0;
    --muted: #8892a4;
    --accent: #6c63ff;
    --green: #22c55e;
    --red: #ef4444;
    --orange: #f97316;
    --yellow: #eab308;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }
  .header { background: var(--card); border-bottom: 1px solid var(--border); padding: 18px 32px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 1.3rem; font-weight: 700; color: var(--text); }
  .header .dot { width: 10px; height: 10px; background: var(--green); border-radius: 50%; box-shadow: 0 0 8px var(--green); }
  .stats-bar { display: flex; gap: 16px; padding: 20px 32px; flex-wrap: wrap; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 24px; flex: 1; min-width: 160px; }
  .stat-card .label { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 1.8rem; font-weight: 700; }
  .stat-card.s-total .value { color: var(--accent); }
  .stat-card.s-active .value { color: var(--green); }
  .stat-card.s-running .value { color: var(--yellow); }
  .stat-card.s-expired .value { color: var(--red); }
  .table-wrap { padding: 0 32px 32px; }
  .table-wrap h2 { font-size: .95rem; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: .06em; }
  .search-bar { margin-bottom: 14px; }
  .search-bar input { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 9px 14px; border-radius: 8px; font-size: .9rem; width: 280px; outline: none; }
  .search-bar input:focus { border-color: var(--accent); }
  table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 12px; overflow: hidden; border: 1px solid var(--border); }
  thead { background: #12141f; }
  th { padding: 12px 14px; text-align: left; font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 13px 14px; font-size: .88rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(108,99,255,.05); }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: .75rem; font-weight: 600; white-space: nowrap; }
  .badge-active { background: rgba(34,197,94,.15); color: #22c55e; border: 1px solid rgba(34,197,94,.3); }
  .badge-expired { background: rgba(239,68,68,.15); color: #ef4444; border: 1px solid rgba(239,68,68,.3); }
  .badge-none { background: rgba(148,163,184,.1); color: #64748b; border: 1px solid rgba(148,163,184,.2); }
  .badge-running { background: rgba(234,179,8,.15); color: #eab308; border: 1px solid rgba(234,179,8,.3); }
  .badge-offline { background: rgba(239,68,68,.1); color: #f87171; border: 1px solid rgba(239,68,68,.2); }
  .num-badge { background: rgba(108,99,255,.2); color: #a5b4fc; padding: 2px 8px; border-radius: 6px; font-size: .8rem; font-weight: 700; }
  .file-list { font-size: .78rem; color: var(--muted); max-width: 200px; }
  .file-chip { display: inline-block; background: rgba(108,99,255,.12); color: #a5b4fc; padding: 1px 7px; border-radius: 5px; margin: 1px; font-size: .72rem; }
  .id-cell { font-family: monospace; font-size: .8rem; color: var(--muted); }
  .empty { text-align: center; padding: 48px; color: var(--muted); }
  .refresh-btn { background: var(--accent); color: #fff; border: none; padding: 9px 18px; border-radius: 8px; cursor: pointer; font-size: .85rem; float: right; margin-bottom: 12px; }
  .refresh-btn:hover { opacity: .85; }
  @media(max-width: 700px) { .stats-bar { padding: 16px; } .table-wrap { padding: 0 12px 24px; } th, td { padding: 10px 8px; } }
</style>
</head>
<body>
<div class="header">
  <div class="dot"></div>
  <h1>🤖 Bot Manager — Tableau de bord Admin</h1>
  <span style="margin-left:auto;font-size:.8rem;color:var(--muted);">Mis à jour : {{ now }}</span>
</div>

<div class="stats-bar">
  <div class="stat-card s-total"><div class="label">Utilisateurs</div><div class="value">{{ stats.total }}</div></div>
  <div class="stat-card s-active"><div class="label">Abonnements actifs</div><div class="value">{{ stats.active }}</div></div>
  <div class="stat-card s-running"><div class="label">Bots en ligne</div><div class="value">{{ stats.running }}</div></div>
  <div class="stat-card s-expired"><div class="label">Expirés / Sans abonnement</div><div class="value">{{ stats.expired }}</div></div>
</div>

<div class="table-wrap">
  <div class="search-bar">
    <input type="text" id="search" placeholder="🔍 Rechercher nom, ID..." oninput="filterTable(this.value)">
    <button class="refresh-btn" onclick="location.reload()">↻ Actualiser</button>
  </div>
  <h2>Enregistrements ({{ projects|length }})</h2>

  {% if projects %}
  <table id="mainTable">
    <thead>
      <tr>
        <th>N° Projet</th>
        <th>Nom / Prénom</th>
        <th>ID Telegram</th>
        <th>Fichier ZIP</th>
        <th>Date d'ajout</th>
        <th>Abonnement restant</th>
        <th>Statut Bot</th>
        <th>📥 ZIP</th>
      </tr>
    </thead>
    <tbody>
      {% for p in projects %}
      <tr>
        <td><span class="num-badge">N° {{ p.project_number }}</span></td>
        <td>
          <div style="font-weight:600;">{{ p.prenom }} {{ p.nom }}</div>
        </td>
        <td class="id-cell">{{ p.telegram_id }}</td>
        <td>
          <div class="file-list">
            <span class="file-chip">main.py</span>
            {% for fname in p.extra_names %}
              <span class="file-chip">{{ fname }}</span>
            {% endfor %}
            {% if p.env_count > 0 %}
              <span class="file-chip" style="color:#6ee7b7;">.env ({{ p.env_count }} vars)</span>
            {% endif %}
          </div>
        </td>
        <td style="white-space:nowrap;font-size:.82rem;">{{ p.date_str }}</td>
        <td>
          <span class="badge {{ p.sub.badge }}">{{ p.sub.label }}</span>
        </td>
        <td>
          {% if p.is_running %}
            <span class="badge badge-running">🟢 En ligne</span>
          {% else %}
            <span class="badge badge-offline">🔴 Hors ligne</span>
          {% endif %}
        </td>
        <td>
          {% if p.zip_available %}
            <a href="/zip/{{ p.telegram_id }}/{{ p.zip_safe_name }}?token={{ token }}"
               style="color:#6c63ff;font-size:.82rem;text-decoration:none;"
               title="Télécharger le ZIP de {{ p.project_name }}">
              ⬇️ {{ p.project_name }}
            </a>
          {% else %}
            <span style="color:#8892a4;font-size:.78rem;">—</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">Aucun utilisateur enregistré pour le moment.</div>
  {% endif %}
</div>

<script>
function filterTable(q) {
  q = q.toLowerCase();
  document.querySelectorAll('#mainTable tbody tr').forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}
</script>
</body>
</html>
"""


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/")
def dashboard():
    if not _auth(request):
        return (
            "<html><body style='background:#0f1117;color:#e2e8f0;font-family:system-ui;"
            "display:flex;align-items:center;justify-content:center;height:100vh;'>"
            "<div style='text-align:center'><h2>🔐 Accès refusé</h2>"
            "<p style='color:#8892a4;margin-top:8px'>Token requis : <code>?token=VOTRE_TOKEN</code></p></div></body></html>",
            403,
        )

    raw_projects = get_all_projects()
    projects = []
    stats = {"total": len(raw_projects), "active": 0, "running": 0, "expired": 0}

    for p in raw_projects:
        tid = p["telegram_id"]
        sub = _sub_info(p)
        active = sub["days"] > 0 or (sub["days"] == -1 and False)
        active = is_subscription_active(tid)

        if active:
            stats["active"] += 1
        else:
            stats["expired"] += 1
        if p["is_running"]:
            stats["running"] += 1

        extra = p.get("extra_files") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        extra_names = list(extra.keys()) if isinstance(extra, dict) else []

        env = p.get("env_vars") or {}
        if isinstance(env, str):
            try:
                env = json.loads(env)
            except Exception:
                env = {}
        env_count = len(env) if isinstance(env, dict) else 0

        date_val = p.get("date_creation")
        date_str = date_val.strftime("%d/%m/%Y %H:%M") if date_val else "—"

        pname      = p.get("project_name", "")
        safe_name  = "".join(c if c.isalnum() or c == "_" else "_" for c in pname.lower())
        zip_path   = os.path.join(UPLOAD_DIR, f"{p['telegram_id']}_{safe_name}.zip")

        projects.append({
            "project_number": p["project_number"],
            "project_name":   pname,
            "nom":            p["nom"],
            "prenom":         p["prenom"],
            "telegram_id":    p["telegram_id"],
            "is_running":     p["is_running"],
            "date_str":       date_str,
            "extra_names":    extra_names,
            "env_count":      env_count,
            "sub":            sub,
            "zip_available":  os.path.exists(zip_path),
            "zip_safe_name":  safe_name,
        })

    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    return render_template_string(
        DASHBOARD_HTML,
        projects=projects,
        stats=stats,
        now=now_str,
        token=request.args.get("token", ""),
    )


@app.route("/zip/<int:tid>/<safe_name>")
def download_zip(tid: int, safe_name: str):
    """Téléchargement du ZIP d'un bot — admin seulement."""
    if not _auth(request):
        abort(403)
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in safe_name)
    path = os.path.join(UPLOAD_DIR, f"{tid}_{safe}.zip")
    if not os.path.exists(path):
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=f"bot_{tid}_{safe}.zip",
        mimetype="application/zip",
    )


# ─────────────────────────────────────────────────────────────────────────────
# PROXY INVERSE — Sites web hébergés
# URL :  /site/<telegram_id>/<slug>/          (sans auth — public)
#        /site/<telegram_id>/<slug>/<path>
# ─────────────────────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9_]{1,48}$")

def _safe_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().strip())[:48]


def _proxy_error(title: str, detail: str, code: int = 502):
    return (
        f"<html><body style='background:#0f1117;color:#e2e8f0;"
        f"font-family:system-ui;display:flex;align-items:center;"
        f"justify-content:center;height:100vh;'>"
        f"<div style='text-align:center;max-width:480px'>"
        f"<h2 style='margin-bottom:10px'>{title}</h2>"
        f"<p style='color:#8892a4'>{detail}</p>"
        f"</div></body></html>",
        code,
    )


@app.route("/site/<int:tid>/<slug>/", defaults={"path": ""}, methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
@app.route("/site/<int:tid>/<slug>/<path:path>",            methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
def proxy_site(tid: int, slug: str, path: str):
    """Proxy transparent vers le site web de l'utilisateur (port interne)."""
    if not _SLUG_RE.match(slug):
        return _proxy_error("❌ Nom de projet invalide", "L'identifiant du projet contient des caractères non autorisés.", 400)

    # Rechercher le projet dans la DB (par slug = safe project_name)
    try:
        bots = get_user_bots(tid)
        project = None
        for b in bots:
            if _safe_slug(b["project_name"]) == slug:
                project = b
                break
    except Exception as e:
        return _proxy_error("❌ Erreur base de données", str(e), 503)

    if not project:
        return _proxy_error(
            "❌ Projet introuvable",
            f"Aucun projet <code>{slug}</code> pour cet utilisateur.",
            404,
        )

    if project.get("project_type", "bot") != "website":
        return _proxy_error("❌ Pas un site web", "Ce projet est un bot Telegram, pas un site web.", 400)

    if not project.get("is_running"):
        return _proxy_error(
            "⚠️ Site hors ligne",
            "Ce site n'est pas démarré. Lancez-le via le bot manager.",
            503,
        )

    # Récupérer le port assigné
    port = get_bot_assigned_port(tid, project["project_name"])
    if not port:
        return _proxy_error("⚠️ Port non assigné", "Le site n'a pas encore de port attribué. Redémarrez-le.", 503)

    # Construire l'URL cible
    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.query_string:
        target_url += "?" + request.query_string.decode("utf-8", errors="replace")

    # Forwarder la requête
    headers = {k: v for k, v in request.headers if k.lower() not in
               ("host", "content-length", "transfer-encoding", "connection")}
    headers["X-Forwarded-For"]   = request.remote_addr or ""
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Real-IP"]         = request.remote_addr or ""

    req_body = request.get_data()

    # Tentatives avec délai — gère le cas où le site vient juste de démarrer
    _MAX_RETRIES  = 5
    _RETRY_DELAYS = [1, 1, 2, 2, 2]  # secondes entre chaque essai

    last_error = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _requests.request(
                method  = request.method,
                url     = target_url,
                headers = headers,
                data    = req_body,
                cookies = request.cookies,
                allow_redirects = False,
                timeout = 30,
                stream  = True,
            )
            # Nettoyer les headers de la réponse
            excluded = {"transfer-encoding", "connection", "content-encoding"}
            out_headers = [(k, v) for k, v in resp.raw.headers.items()
                           if k.lower() not in excluded]
            return Response(
                stream_with_context(resp.iter_content(chunk_size=4096)),
                status  = resp.status_code,
                headers = out_headers,
            )
        except _requests.exceptions.ConnectionError as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
            continue
        except _requests.exceptions.Timeout:
            return _proxy_error("⏱ Délai dépassé", "Le site n'a pas répondu dans les délais.", 504)
        except Exception as e:
            return _proxy_error("❌ Erreur proxy", str(e), 502)

    # Toutes les tentatives ont échoué
    return _proxy_error(
        "⚠️ Site inaccessible",
        f"Le site sur le port {port} ne répond pas après {_MAX_RETRIES} tentatives. "
        "Il a peut-être planté — redémarrez-le via le bot manager.",
        502,
    )
