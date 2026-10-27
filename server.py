#!/usr/bin/env python3
"""Serveur MCP (stdio) : donne à l'agent Hermes l'accès aux flows Flowise.

Deux outils :
  flowise_list_flows()                      liste les flows autorisés (entrée attendue, documentation, nœuds)
  flowise_run_flow(flow, question[, session_id[, verbose]])   exécute un flow et renvoie sa réponse

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

Convention de documentation : la première ligne non vide de la première note autocollante (Sticky Note) du flow
devient sa description dans flowise_list_flows. Rien à configurer : une note en tête de flow suffit.
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

VERSION = "0.2.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
HERMES_NODES = ("hermesTaskAgentflow",)
SCHEDULE_NODES = ("hermesScheduleAgentflow",)
NOTE_NODES = ("stickyNoteAgentflow",)
INPUT_LABELS = {"chatInput": "texte libre", "formInput": "formulaire", "webhookTrigger": "webhook", "scheduleInput": "planifié"}
MAX_DETAIL = 400


class ToolError(Exception):
    """Erreur lisible renvoyée à l'agent (isError)."""


def load_config(env=None):
    env = os.environ if env is None else env
    allow = [p.strip() for p in (env.get("FLOWISE_MCP_ALLOW") or "mcp-*").split(",") if p.strip()]
    return {
        "url": (env.get("FLOWISE_URL") or "http://localhost:3000").rstrip("/"),
        "key": env.get("FLOWISE_API_KEY") or "",
        "allow": allow,
        "allow_schedule": (env.get("FLOWISE_MCP_ALLOW_SCHEDULE") or "") in ("1", "true", "yes"),
        "timeout": float(env.get("FLOWISE_MCP_TIMEOUT") or 300),
        "max_concurrent": int(env.get("FLOWISE_MCP_MAX_CONCURRENT") or 2),
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


def all_flows(cfg):
    flows = http_json("GET", f"{cfg['url']}/api/v1/chatflows", cfg["key"])
    if not isinstance(flows, list):
        raise ToolError("Réponse inattendue de Flowise (liste de flows attendue).")
    return [describe(f, cfg) for f in flows]


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
        records = http_json("GET", f"{cfg['url']}/api/v1/executions?chatflowId={flow_id}", cfg["key"], timeout=30)
    except ToolError:
        return None
    data = records.get("data") if isinstance(records, dict) else records
    if not isinstance(data, list) or not data:
        return None

    def stamp(record):
        try:
            # Flowise date ses exécutions en UTC (« 2026-10-26T22:05:11.000Z ») ; on compare à un
            # horodatage epoch, donc il faut marquer le fuseau, sinon le décalage local fausse le filtre.
            parsed = datetime.datetime.strptime(record["createdDate"], "%Y-%m-%dT%H:%M:%S.%fZ")
            return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()
        except (KeyError, TypeError, ValueError):
            return 0.0

    ordered = sorted(data, key=stamp, reverse=True)
    if since:
        recent = [r for r in ordered if stamp(r) >= since - 15]  # écarte l'exécution d'un appel concurrent
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
            "instructions": "Exécute des flows Flowise : flowise_list_flows puis flowise_run_flow.",
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


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest(load_config()))
    serve()
