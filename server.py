#!/usr/bin/env python3
"""Serveur MCP (stdio) : donne à l'agent Hermes l'accès aux flows Flowise.

Deux outils :
  flowise_list_flows()                      liste les flows autorisés
  flowise_run_flow(flow, question[, session_id])   exécute un flow et renvoie sa réponse

Bibliothèque standard uniquement (aucun SDK MCP : le protocole stdio est du JSON-RPC 2.0, un message par ligne).

Configuration (variables d'environnement) :
  FLOWISE_URL           défaut http://localhost:3000
  FLOWISE_API_KEY       clé Flowise (Flowise > API Keys)
  FLOWISE_MCP_ALLOW     motifs de noms de flows autorisés, séparés par des virgules (défaut « mcp-* » ; « * » = tous)
  FLOWISE_MCP_ALLOW_SCHEDULE=1   autorise aussi les flows contenant un nœud « Hermes Schedule » (gestion de jobs)
  FLOWISE_MCP_TIMEOUT   délai d'un appel de flow en secondes (défaut 300)
  FLOWISE_MCP_MAX_CONCURRENT   appels de flows simultanés (défaut 2)
"""

import fnmatch
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
HERMES_NODES = ("hermesTaskAgentflow",)
SCHEDULE_NODES = ("hermesScheduleAgentflow",)


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


def node_names(flow):
    try:
        return {n.get("data", {}).get("name") for n in json.loads(flow.get("flowData") or "{}").get("nodes", [])}
    except ValueError:
        return set()


def describe(flow, cfg):
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
            "manages_jobs": manages_jobs, "allowed": reason is None, "reason": reason}


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
        note = " [appelle Hermes : ne pas le rappeler depuis ce flow, risque de boucle]" if f["calls_hermes"] else ""
        lines.append(f"- {f['name']} (id {f['id']}, {f['type']}){note}")
    return "Flows disponibles :\n" + "\n".join(lines)


_slots = None


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
    try:
        body = {"question": question, "streaming": False}
        if args.get("session_id"):
            body["overrideConfig"] = {"sessionId": str(args["session_id"])}
        data = http_json("POST", f"{cfg['url']}/api/v1/prediction/{flow['id']}", cfg["key"], body, timeout=cfg["timeout"])
    finally:
        _slots.release()

    text = data.get("text") if isinstance(data, dict) else None
    if isinstance(text, str):
        return text.strip() or "(le flow a répondu vide)"
    return json.dumps(data, ensure_ascii=False)[:4000]


TOOLS = {
    "flowise_list_flows": {
        "description": "Liste les flows Flowise que tu peux exécuter (nom, id, type).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_list_flows,
    },
    "flowise_run_flow": {
        "description": "Exécute un flow Flowise avec une question (texte libre) et renvoie sa réponse. "
                       "Utilise d'abord flowise_list_flows. Un flow peut durer plusieurs minutes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow": {"type": "string", "description": "Nom ou id du flow"},
                "question": {"type": "string", "description": "Entrée envoyée au flow (sujet, critère, demande…)"},
                "session_id": {"type": "string", "description": "Optionnel : identifiant de session pour garder la mémoire d'une conversation"},
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
