#!/usr/bin/env python3
"""Serveur MCP (stdio) : donne à l'agent Hermes l'accès aux flows Flowise.

Trois outils :
  flowise_list_flows()                      liste les flows autorisés (entrée attendue, documentation, nœuds)
  flowise_run_flow(flow, question[, session_id[, verbose]])   exécute un flow et renvoie sa réponse
  flowise_doctor([all])                     état de santé, en lecture seule (aucun flow exécuté)

En cas d'échec, la réponse de Flowise ne dit pas quel nœud a lâché : le serveur relit donc la dernière
exécution enregistrée (GET /api/v1/executions?chatflowId=…) pour nommer le nœud fautif et son erreur.
`verbose=true` ajoute la trace des nœuds à une exécution réussie.

Bibliothèque standard uniquement (aucun SDK MCP : le protocole stdio est du JSON-RPC 2.0, un message par ligne).

Configuration (variables d'environnement) :
  FLOWISE_URL           défaut http://localhost:3000
  FLOWISE_API_KEY       clé Flowise (Flowise > API Keys)
  FLOWISE_MCP_ALLOW     motifs de noms de flows autorisés, séparés par des virgules (défaut « mcp-* » ; « * » = tous)
  FLOWISE_MCP_ALLOW_SCHEDULE=1   autorise aussi les flows contenant un nœud « Hermes Schedule » (gestion de jobs)
  FLOWISE_MCP_TIMEOUT   délai d'un appel de flow en secondes (défaut 300)
  FLOWISE_MCP_MAX_CONCURRENT   appels de flows simultanés (défaut 2)
  FLOWISE_MCP_DOCTOR_IGNORE    noms de jobs Hermes à ne pas signaler par le diagnostic (séparés par des virgules)

Convention de documentation : la première ligne non vide de la première note autocollante (Sticky Note) du flow
devient sa description dans flowise_list_flows. Rien à configurer : une note en tête de flow suffit.

Ligne de commande : `--selftest` (vérifie la configuration) et `--doctor [--all]` (état de santé ; sortie vide
quand tout va bien, ce qui en fait un script de surveillance utilisable tel quel par une tâche planifiée).
"""

import datetime
import fnmatch
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

VERSION = "0.3.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
HERMES_NODES = ("hermesTaskAgentflow",)
SCHEDULE_NODES = ("hermesScheduleAgentflow",)
NOTE_NODES = ("stickyNoteAgentflow",)
INPUT_LABELS = {"chatInput": "texte libre", "formInput": "formulaire", "webhookTrigger": "webhook", "scheduleInput": "planifié"}
MAX_DETAIL = 400
DOCTOR_GRACE_S = 3 * 3600  # retard toléré avant de signaler un job (le planificateur a son propre rattrapage)
EXECUTION_PAGE = 200  # taille de page demandée à l'API des exécutions (les plus récentes d'abord)


class ToolError(Exception):
    """Erreur lisible renvoyée à l'agent (isError)."""


def load_config(env=None):
    env = os.environ if env is None else env
    allow = [p.strip() for p in (env.get("FLOWISE_MCP_ALLOW") or "mcp-*").split(",") if p.strip()]
    ignore = [p.strip() for p in (env.get("FLOWISE_MCP_DOCTOR_IGNORE") or "").split(",") if p.strip()]
    return {
        "url": (env.get("FLOWISE_URL") or "http://localhost:3000").rstrip("/"),
        "key": env.get("FLOWISE_API_KEY") or "",
        "allow": allow,
        "allow_schedule": (env.get("FLOWISE_MCP_ALLOW_SCHEDULE") or "") in ("1", "true", "yes"),
        "timeout": float(env.get("FLOWISE_MCP_TIMEOUT") or 300),
        "max_concurrent": int(env.get("FLOWISE_MCP_MAX_CONCURRENT") or 2),
        "ignore_jobs": ignore,
    }


def http_json(method, url, key, body=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace") or "null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in (401, 403):
            raise ToolError(f"Flowise a refusé l'accès (HTTP {e.code}) : vérifiez FLOWISE_API_KEY. {detail}")
        if e.code == 404:
            raise ToolError(f"Introuvable côté Flowise (HTTP 404). {detail}")
        raise ToolError(f"Flowise a répondu HTTP {e.code}. {detail}")
    except urllib.error.URLError as e:
        raise ToolError(f"Flowise injoignable à {url} ({e.reason}). Le service tourne-t-il ? Vérifiez FLOWISE_URL.")
    except TimeoutError:
        raise ToolError(f"Flowise n'a pas répondu en {int(timeout)} s.")
    except ValueError:
        raise ToolError("Réponse de Flowise non JSON.")


def parse_date(value):
    """Date ISO (Flowise ou Hermes) -> horodatage epoch, ou None si illisible.

    Les deux sources datent en UTC, avec un suffixe « Z » ou un décalage explicite.
    """
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def human_delta(seconds):
    """Durée lisible : « 42 s », « 12 min », « 3 h 05 », « 4 j »."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    heures = minutes // 60
    if heures < 48:
        return f"{heures} h" + (f" {minutes % 60:02d}" if minutes % 60 else "")
    return f"{heures // 24} j"


def flow_nodes(flow):
    """Nœuds du flow ; liste vide si le flowData est illisible."""
    try:
        return json.loads(flow.get("flowData") or "{}").get("nodes", [])
    except ValueError:
        return []


def node_names(flow):
    return {n.get("data", {}).get("name") for n in flow_nodes(flow)}


def input_contract(nodes):
    """Entrée attendue, déduite du nœud Start : texte libre, formulaire (avec ses champs), webhook ou planifié."""
    start = next((n.get("data", {}) for n in nodes if n.get("data", {}).get("name") == "startAgentflow"), None)
    if start is None:
        return None
    inputs = start.get("inputs") or {}
    kind = inputs.get("startInputType") or "chatInput"
    label = INPUT_LABELS.get(kind, kind)
    if kind == "formInput":
        champs = [str(c["name"]) for c in (inputs.get("formInputTypes") or []) if isinstance(c, dict) and c.get("name")]
        if champs:
            label += " (" + ", ".join(champs) + ")"
    return label


def summary(nodes):
    """Description du flow : première ligne non vide de sa première note autocollante."""
    for n in nodes:
        data = n.get("data", {})
        if data.get("name") in NOTE_NODES:
            for ligne in ((data.get("inputs") or {}).get("note") or "").splitlines():
                if ligne.strip():
                    return ligne.strip()[:160]
    return None


def describe(flow, cfg):
    nodes = flow_nodes(flow)
    names = node_names(flow)
    matches = any(fnmatch.fnmatchcase(flow["name"].lower(), p.lower()) or flow["id"] == p for p in cfg["allow"])
    manages_jobs = bool(names & set(SCHEDULE_NODES))
    if not matches:
        reason = "nom hors de FLOWISE_MCP_ALLOW"
    elif manages_jobs and not cfg["allow_schedule"]:
        reason = "contient un nœud Hermes Schedule (FLOWISE_MCP_ALLOW_SCHEDULE=1 pour l'autoriser)"
    else:
        reason = None
    return {"id": flow["id"], "name": flow["name"], "type": flow.get("type"), "calls_hermes": bool(names & set(HERMES_NODES)),
            "manages_jobs": manages_jobs, "allowed": reason is None, "reason": reason,
            "input": input_contract(nodes), "summary": summary(nodes),
            "steps": sum(1 for n in nodes if n.get("data", {}).get("name") not in NOTE_NODES)}


def raw_flows(cfg):
    """Flows tels que Flowise les renvoie (flowData inclus, contrairement à all_flows)."""
    flows = http_json("GET", f"{cfg['url']}/api/v1/chatflows", cfg["key"])
    if not isinstance(flows, list):
        raise ToolError("Réponse inattendue de Flowise (liste de flows attendue).")
    return flows


def all_flows(cfg):
    return [describe(f, cfg) for f in raw_flows(cfg)]


def tool_list_flows(args, cfg):
    allowed = [f for f in all_flows(cfg) if f["allowed"]]
    if not allowed:
        return (f"Aucun flow autorisé. Seuls les flows dont le nom correspond à {', '.join(cfg['allow'])} sont exposés : "
                "renommez un flow (ex. « mcp-actu-ia ») ou élargissez FLOWISE_MCP_ALLOW.")
    lines = []
    for f in allowed:
        parts = [f"- {f['name']} (id {f['id']}, {f['type']})"]
        if f["input"]:
            parts.append(f"entrée : {f['input']}")
        parts.append(f"{f['steps']} nœud" + ("s" if f["steps"] > 1 else ""))
        if f["summary"]:
            parts.append(f"doc : « {f['summary']} »")
        if f["calls_hermes"]:
            parts.append("[appelle Hermes : ne pas le rappeler depuis ce flow, risque de boucle]")
        lines.append(" · ".join(parts))
    return "Flows disponibles :\n" + "\n".join(lines)


_slots = None


def last_execution(cfg, flow_id, since=None):
    """Dernière exécution enregistrée par Flowise pour ce flow (elle contient la trace par nœud).

    La réponse de /prediction ne contient rien quand un nœud échoue : c'est ici que se trouve le nœud fautif.
    Renvoie None si l'API des exécutions est indisponible (version ancienne, droits, panne) — ne lève jamais.
    """
    try:
        records = http_json("GET", f"{cfg['url']}/api/v1/executions?agentflowId={flow_id}&limit={EXECUTION_PAGE}", cfg["key"], timeout=30)
    except ToolError:
        return None
    data = records.get("data") if isinstance(records, dict) else records
    if not isinstance(data, list):
        return None
    # Le filtre serveur est « agentflowId » (« chatflowId » est ignoré par certaines versions), et on
    # refiltre quand même ici : sans ça, l'échec d'un AUTRE flow serait attribué à celui-ci.
    data = [r for r in data if r.get("agentflowId") in (None, flow_id)]
    if not data:
        return None
    ordered = sorted(data, key=lambda r: parse_date(r.get("createdDate")) or 0.0, reverse=True)
    if since:
        # Écarte l'exécution d'un appel concurrent : on ne garde que ce qui est postérieur au lancement.
        recent = [r for r in ordered if (parse_date(r.get("createdDate")) or 0.0) >= since - 15]
        if recent:
            return recent[0]
    return ordered[0]


def execution_trace(record):
    """Trace par nœud d'une exécution, ou None si elle est illisible."""
    try:
        trace = json.loads(record.get("executionData") or "[]")
    except ValueError:
        return None
    return trace if isinstance(trace, list) else None


def render_trace(trace):
    """Trace compacte : un nœud par ligne."""
    return "\n".join(f"  - {t.get('nodeId')} ({t.get('nodeLabel')}) : {t.get('status')}" for t in trace)


def diagnosis(cfg, flow_id, since):
    """Après un échec : nomme le nœud fautif et son erreur d'après la dernière exécution enregistrée."""
    record = last_execution(cfg, flow_id, since)
    trace = execution_trace(record) if record else None
    if not trace:
        return ""
    failed = [t for t in trace if t.get("status") == "ERROR"]
    if not failed:
        return ""
    details = []
    for t in failed:
        message = str((t.get("data") or {}).get("error") or "sans détail")[:MAX_DETAIL]
        details.append(f"nœud « {t.get('nodeLabel') or t.get('nodeId')} » ({t.get('nodeId')}) : {message}")
    done = [t.get("nodeId") for t in trace if t.get("status") == "FINISHED"]
    text = "\nTrace Flowise : " + " ; ".join(details)
    if done:
        text += f"\nDernier nœud réussi : {done[-1]}"
    return text


def tool_run_flow(args, cfg):
    global _slots
    flow_ref = str(args.get("flow") or "").strip()
    question = str(args.get("question") or "").strip()
    if not flow_ref:
        raise ToolError("Paramètre « flow » manquant (nom ou id : voir flowise_list_flows).")
    if not question:
        raise ToolError("Paramètre « question » manquant.")

    flows = all_flows(cfg)
    found = [f for f in flows if f["id"] == flow_ref or f["name"].lower() == flow_ref.lower()]
    if not found:
        raise ToolError(f"Aucun flow « {flow_ref} ». Utilisez flowise_list_flows.")
    if len(found) > 1:
        raise ToolError(f"Plusieurs flows s'appellent « {flow_ref} » : utilisez l'id ({', '.join(f['id'] for f in found)}).")
    flow = found[0]
    if not flow["allowed"]:
        raise ToolError(f"Flow « {flow['name']} » non autorisé : {flow['reason']}.")

    if _slots is None:
        _slots = threading.BoundedSemaphore(cfg["max_concurrent"])
    if not _slots.acquire(blocking=False):
        raise ToolError(f"Trop d'appels de flows simultanés (maximum {cfg['max_concurrent']}). Réessayez dans un instant.")
    started, error, data = time.time(), None, None
    try:
        body = {"question": question, "streaming": False}
        if args.get("session_id"):
            body["overrideConfig"] = {"sessionId": str(args["session_id"])}
        data = http_json("POST", f"{cfg['url']}/api/v1/prediction/{flow['id']}", cfg["key"], body, timeout=cfg["timeout"])
    except ToolError as e:
        error = e
    finally:
        _slots.release()

    if error is not None:
        # L'erreur d'origine n'est jamais masquée : le diagnostic vient en complément, ou pas du tout.
        raise ToolError(f"{error}{diagnosis(cfg, flow['id'], started)}")

    text = data.get("text") if isinstance(data, dict) else None
    result = text.strip() if isinstance(text, str) and text.strip() else None
    if result is None:
        result = "(le flow a répondu vide)" if isinstance(text, str) else json.dumps(data, ensure_ascii=False)[:4000]
    if args.get("verbose"):
        trace = data.get("agentFlowExecutedData") if isinstance(data, dict) else None
        if isinstance(trace, list) and trace:
            result += "\n\nTrace des nœuds :\n" + render_trace(trace)
    return result


def preflight(nodes):
    """Contrôles structurels : ce qui casse un flow sans prévenir. Aucun flow n'est exécuté.

    Chaque règle correspond à une panne réellement rencontrée (identifiant laissé en objet -> HTTP 500
    « reading 'trim' », URL vide, fonction vide) ou à un flow qui ne peut rien renvoyer d'exploitable.
    """
    problemes = []
    noms = [n.get("data", {}).get("name") for n in nodes]
    if "startAgentflow" not in noms:
        problemes.append("aucun nœud Start")
    if not any(n.get("data", {}).get("name") == "directReplyAgentflow" or n.get("data", {}).get("hideOutput") for n in nodes):
        problemes.append("aucun nœud de réponse (Direct Reply) : le flow ne renverra rien d'exploitable")
    for n in nodes:
        data = n.get("data", {})
        label = data.get("label") or data.get("id") or data.get("name")
        inputs = data.get("inputs") or {}
        if isinstance(data.get("credential"), dict):
            problemes.append(f"nœud « {label} » : identifiant mal formé (objet au lieu de l'id) — le flow échouera à l'exécution")
        elif data.get("credential") == "" and data.get("name") in HERMES_NODES + SCHEDULE_NODES:
            problemes.append(f"nœud « {label} » : aucun identifiant « Hermes API » sélectionné")
        if data.get("name") == "httpAgentflow" and not str(inputs.get("url") or "").strip():
            problemes.append(f"nœud « {label} » : URL vide")
        if data.get("name") == "customFunctionAgentflow" and not str(inputs.get("customFunctionJavascriptFunction") or "").strip():
            problemes.append(f"nœud « {label} » : fonction JavaScript vide")
        if data.get("name") == "hermesTaskAgentflow" and not str(inputs.get("hermesPrompt") or "").strip():
            problemes.append(f"nœud « {label} » : tâche vide")
    return problemes


def hermes_home():
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def hermes_jobs():
    """Jobs du planificateur Hermes (lecture seule). Liste vide si le fichier est absent ou illisible."""
    try:
        with open(os.path.join(hermes_home(), "cron", "jobs.json"), encoding="utf-8") as handle:
            jobs = json.load(handle).get("jobs", [])
    except (OSError, ValueError):
        return []
    return jobs if isinstance(jobs, list) else []


def job_interval_seconds(job):
    """Période d'un job à intervalle, en secondes, ou None si la cadence n'est pas un intervalle simple."""
    schedule = job.get("schedule") or {}
    if schedule.get("kind") == "interval" and schedule.get("minutes"):
        try:
            return float(schedule["minutes"]) * 60
        except (TypeError, ValueError):
            return None
    return None


def job_issues(jobs, now=None, ignore=()):
    """Pannes silencieuses du planificateur : job en pause, en erreur, ou en retard.

    C'est le cœur du diagnostic : un job qui ne tourne pas ne produit aucun message, donc rien ne se voit.
    Un job en pause est signalé volontairement — oublier de reprendre un job, c'est perdre son alerte.
    """
    now = time.time() if now is None else now
    ignores = {n.strip().lower() for n in ignore if n.strip()}
    issues = []
    for job in jobs:
        nom = job.get("name") or job.get("id")
        if (nom or "").lower() in ignores:
            continue
        if not job.get("enabled", True) or job.get("state") == "paused":
            issues.append(("ATTENTION", f"job « {nom} » : en pause depuis {job.get('paused_at') or 'date inconnue'} — aucune alerte ne partira"))
            continue
        if job.get("failure_streak"):
            issues.append(("ÉCHEC", f"job « {nom} » : {job['failure_streak']} échec(s) consécutif(s) — {str(job.get('last_error') or '')[:200]}"))
        elif job.get("last_status") not in (None, "ok"):
            issues.append(("ÉCHEC", f"job « {nom} » : dernier statut « {job['last_status']} » — {str(job.get('last_error') or '')[:200]}"))
        prevu = parse_date(job.get("next_run_at"))
        if prevu:
            periode = job_interval_seconds(job)
            tolerance = max(DOCTOR_GRACE_S, 3 * periode) if periode else DOCTOR_GRACE_S
            if now - prevu > tolerance:
                issues.append(("ÉCHEC", f"job « {nom} » : en retard de {human_delta(now - prevu)} (passage prévu le {job.get('next_run_at')})"))
    return issues


def format_problems(problems):
    """Échecs d'abord, puis les avertissements."""
    rang = {"ÉCHEC": 0, "ATTENTION": 1}
    return "\n".join(f"{sev:9s} {msg}" for sev, msg in sorted(problems, key=lambda p: rang.get(p[0], 2)))


def doctor_report(cfg, show_all=False):
    """État de santé en LECTURE SEULE : rien n'est exécuté, rien n'est modifié.

    Renvoie (problèmes, détails) : les problèmes sont des couples (sévérité, message), les détails
    décrivent les éléments sains (affichés seulement avec show_all).
    """
    problemes, details, now = [], [], time.time()
    try:
        flows = raw_flows(cfg)
    except ToolError as e:
        return [("ÉCHEC", f"Flowise : {e}")], []

    exposes = []
    for flow in flows:
        if not describe(flow, cfg)["allowed"]:
            continue
        exposes.append(flow)
        for pb in preflight(flow_nodes(flow)):
            problemes.append(("ATTENTION", f"flow « {flow['name']} » : {pb}"))
        record = last_execution(cfg, flow["id"])
        if record is None:
            details.append(f"  flow « {flow['name']} » : aucune exécution enregistrée")
            continue
        etat = record.get("state")
        quand = human_delta(now - (parse_date(record.get("createdDate")) or now))
        if etat == "ERROR":
            trace = execution_trace(record) or []
            detail = " ; ".join(
                f"nœud « {t.get('nodeLabel') or t.get('nodeId')} » : {str((t.get('data') or {}).get('error') or 'sans détail')[:200]}"
                for t in trace if t.get("status") == "ERROR")
            problemes.append(("ATTENTION", f"flow « {flow['name']} » : dernière exécution en erreur il y a {quand}" + (f" — {detail}" if detail else "")))
        else:
            details.append(f"  flow « {flow['name']} » : dernière exécution {etat} il y a {quand}")

    jobs = hermes_jobs()
    if jobs:
        problemes.extend(job_issues(jobs, now, cfg.get("ignore_jobs") or ()))
        details.append(f"  {len(jobs)} job(s) Hermes et {len(exposes)} flow(s) exposé(s) vérifiés")
    else:
        details.append("  jobs Hermes illisibles (cron/jobs.json absent ?)")
    return problemes, details


def tool_doctor(args, cfg):
    problemes, details = doctor_report(cfg, show_all=bool(args.get("all")))
    if problemes:
        return "Problèmes détectés :\n" + format_problems(problemes)
    texte = "Aucun problème détecté : Flowise répond, les flows exposés sont sains, les jobs Hermes sont à jour."
    if args.get("all") and details:
        texte += "\n" + "\n".join(details)
    return texte


TOOLS = {
    "flowise_list_flows": {
        "description": "Liste les flows Flowise que tu peux exécuter (nom, id, type, entrée attendue, documentation, nombre de nœuds).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_list_flows,
    },
    "flowise_run_flow": {
        "description": "Exécute un flow Flowise avec une question (texte libre) et renvoie sa réponse. "
                       "Utilise d'abord flowise_list_flows. Un flow peut durer plusieurs minutes. "
                       "En cas d'échec, le message nomme le nœud fautif ; verbose=true ajoute la trace des nœuds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow": {"type": "string", "description": "Nom ou id du flow"},
                "question": {"type": "string", "description": "Entrée envoyée au flow (sujet, critère, demande…)"},
                "session_id": {"type": "string", "description": "Optionnel : identifiant de session pour garder la mémoire d'une conversation"},
                "verbose": {"type": "boolean", "description": "Optionnel : ajoute la trace des nœuds (statut de chacun) à la réponse"},
            },
            "required": ["flow", "question"],
        },
        "handler": tool_run_flow,
    },
    "flowise_doctor": {
        "description": "Diagnostique l'ensemble, en lecture seule (aucun flow n'est exécuté) : Flowise joignable, flows exposés "
                       "structurellement sains, et jobs Hermes en pause, en erreur ou en retard. À utiliser quand on demande si tout "
                       "va bien, ou pour comprendre pourquoi une alerte attendue n'est pas arrivée.",
        "inputSchema": {
            "type": "object",
            "properties": {"all": {"type": "boolean", "description": "Optionnel : décrit aussi les éléments sains"}},
            "additionalProperties": False,
        },
        "handler": tool_doctor,
    },
}


def handle(message, cfg):
    """Traite un message JSON-RPC ; renvoie la réponse (dict) ou None pour une notification."""
    method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if msg_id is None:  # notification (notifications/initialized, cancelled…)
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "flowise", "version": VERSION},
            "instructions": "Exécute des flows Flowise : flowise_list_flows puis flowise_run_flow. flowise_doctor pour l'état de santé.",
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]} for n, t in TOOLS.items()]})
    if method == "tools/call":
        tool = TOOLS.get(params.get("name"))
        if tool is None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Outil inconnu : {params.get('name')}"}}
        try:
            return ok({"content": [{"type": "text", "text": tool["handler"](params.get("arguments") or {}, cfg)}], "isError": False})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": f"Erreur : {e}"}], "isError": True})
        except Exception as e:  # ne jamais faire tomber le serveur
            return ok({"content": [{"type": "text", "text": f"Erreur inattendue : {type(e).__name__}: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Méthode non gérée : {method}"}}


def serve(stdin=None, stdout=None, cfg=None):
    stdin, stdout, cfg = stdin or sys.stdin, stdout or sys.stdout, cfg or load_config()
    write_lock = threading.Lock()

    def send(payload):
        with write_lock:
            stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stdout.flush()

    def work(message):
        response = handle(message, cfg)
        if response is not None:
            send(response)

    with ThreadPoolExecutor(max_workers=4) as pool:  # un appel de flow long ne bloque pas les autres requêtes
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON invalide"}})
                continue
            if isinstance(message, dict):
                pool.submit(work, message)


def selftest(cfg):
    """Vérifie la configuration sans MCP : code 0 si Flowise répond avec la clé."""
    print(f"serveur flowise-mcp {VERSION} | Flowise : {cfg['url']} | clé : {'définie' if cfg['key'] else 'ABSENTE'} | autorisés : {', '.join(cfg['allow'])}")
    try:
        flows = all_flows(cfg)
    except ToolError as e:
        print(f"ÉCHEC : {e}")
        return 1
    allowed = [f for f in flows if f["allowed"]]
    print(f"OK : {len(flows)} flow(s) dans Flowise, {len(allowed)} exposé(s) à l'agent" + (" : " + ", ".join(f["name"] for f in allowed) if allowed else ""))
    if not allowed:
        print("Aucun flow exposé : renommez un flow en « mcp-… » ou élargissez FLOWISE_MCP_ALLOW.")
    return 0


def doctor_cli(cfg, show_all=False):
    """État de santé en ligne de commande.

    Sortie VIDE quand tout va bien : c'est ce qui en fait un script de surveillance utilisable tel quel
    par une tâche planifiée Hermes (sortie vide = aucun message envoyé).
    """
    problemes, details = doctor_report(cfg, show_all=show_all)
    if problemes:
        print(f"Diagnostic flowise-mcp {VERSION} — {len(problemes)} problème(s) :")
        print(format_problems(problemes))
    elif show_all:
        print(f"Diagnostic flowise-mcp {VERSION} — aucun problème.")
        if details:
            print("\n".join(details))
    return 0


if __name__ == "__main__":
    config = load_config()
    options = sys.argv[1:]
    if "--selftest" in options:
        sys.exit(selftest(config))
    if "--doctor" in options:
        sys.exit(doctor_cli(config, show_all="--all" in options))
    inconnues = [o for o in options if o != "--all"]
    if inconnues:
        # Sans ce garde-fou, une faute de frappe ferait démarrer le serveur stdio, qui lirait une entrée
        # vide et sortirait en 0 : un script de surveillance se tairait au lieu de signaler l'erreur.
        print(f"Option inconnue : {' '.join(inconnues)}. Options acceptées : --selftest, --doctor [--all].", file=sys.stderr)
        sys.exit(2)
    serve()
