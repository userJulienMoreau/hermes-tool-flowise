#!/usr/bin/env python3
"""Installe (ou met à jour, ou retire) l'outil « flowise » de Hermes : un serveur MCP qui exécute des flows Flowise.

  python3 install.py [--hermes-home DIR] [--flowise-url URL] [--api-key CLÉ] [--allow MOTIFS] [--dry-run]
  python3 install.py --uninstall

Ce que fait l'installeur (idempotent, aucune dépendance) :
  1. copie server.py dans <hermes-home>/tools/flowise-mcp/ ;
  2. déclare le serveur sous `mcp_servers:` dans <hermes-home>/config.yaml (sauvegarde, validation, retour arrière si invalide) ;
  3. ajoute FLOWISE_API_KEY à <hermes-home>/.env si fournie (option ou variable d'environnement) et absente ;
  4. vérifie la connexion à Flowise.
Il n'active rien tout seul : à la fin, recharger les outils (/reload-mcp) ou redémarrer la passerelle Hermes.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_NAME = "flowise"
TOOLS = ("flowise_list_flows", "flowise_run_flow")


def default_home():
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def server_block(server_path, url, allow, python):
    return [
        f"  {SERVER_NAME}:",
        f"    command: {python}",
        f"    args: [\"{server_path}\"]",
        "    env:",
        f"      FLOWISE_URL: \"{url}\"",
        "      FLOWISE_API_KEY: ${FLOWISE_API_KEY}",
        f"      FLOWISE_MCP_ALLOW: \"{allow}\"",
        "    timeout: 300",
        "    tools:",
        f"      include: [{', '.join(TOOLS)}]",
    ]


TOP_KEY = re.compile(r"^mcp_servers:\s*(?P<rest>.*?)\s*$")


def section_end(lines, top):
    """Fin de la section `mcp_servers:` : première ligne non vide, non commentée, non indentée."""
    return next((i for i in range(top + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "#"))), len(lines))


def find_block(lines):
    """(indice de `mcp_servers:`, début, fin) du bloc flowise ; début/fin = None si absent."""
    top = next((i for i, l in enumerate(lines) if TOP_KEY.match(l)), None)
    if top is None:
        return None, None, None
    end_top = section_end(lines, top)
    start = next((i for i in range(top + 1, end_top) if re.match(rf"^  {SERVER_NAME}:\s*(#.*)?$", lines[i])), None)
    if start is None:
        return top, None, None
    end = next((i for i in range(start + 1, end_top) if lines[i].strip() and not lines[i].startswith("   ") and not lines[i].lstrip().startswith("#")), end_top)
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return top, start, end


def add_block(text, block):
    lines = text.split("\n")
    top, start, end = find_block(lines)
    if start is not None:  # mise à jour : on remplace le bloc existant
        lines[start:end] = block
        return "\n".join(lines)
    if top is not None:
        rest = TOP_KEY.match(lines[top]).group("rest")
        if rest and not rest.startswith("#"):  # « mcp_servers: {} » ou « null » : on remplace la ligne
            if rest not in ("{}", "null", "~"):
                raise ValueError(f"`mcp_servers:` a une forme inattendue ({rest!r}) : ajoutez le serveur à la main (voir INSTALL.md).")
            lines[top] = "mcp_servers:"
        lines[top + 1:top + 1] = block
        return "\n".join(lines)
    suffix = "" if text.endswith("\n") or not text else "\n"
    return text + suffix + ("\n" if text else "") + "mcp_servers:\n" + "\n".join(block) + "\n"


def remove_block(text):
    lines = text.split("\n")
    top, start, end = find_block(lines)
    if start is None:
        return text, False
    del lines[start:end]
    body = lines[top + 1:section_end(lines, top)]
    if not any(l.strip() and not l.lstrip().startswith("#") for l in body):  # plus aucun serveur : on garde une clé valide
        lines[top] = "mcp_servers: {}"
    return "\n".join(lines), True


def load_yaml(text):
    """Analyse YAML avec PyYAML ou, à défaut, ruamel.yaml (utilisé par Hermes). (None, False) si aucun n'est disponible."""
    try:
        import yaml
        return yaml.safe_load(text), True
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return None, False
    return YAML(typ="safe").load(text), True


def validate(text):
    """Contrôle la syntaxe du fichier modifié quand une bibliothèque YAML est disponible. Renvoie un message d'erreur ou None."""
    try:
        data, available = load_yaml(text)
    except Exception as e:
        return f"YAML invalide : {e}"
    if not available:
        return None
    entry = ((data or {}).get("mcp_servers") or {}).get(SERVER_NAME)
    if entry is not None and not (entry.get("command") and entry.get("args")):
        return "entrée flowise incomplète après modification"
    return None


def update_env_file(path, key, dry):
    """Ajoute FLOWISE_API_KEY à .env si elle n'y est pas déjà. N'affiche jamais la clé."""
    existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if re.search(r"^FLOWISE_API_KEY=.+", existing, re.M):
        return "FLOWISE_API_KEY déjà présente dans .env (inchangée)"
    if not key:
        return "FLOWISE_API_KEY absente : à ajouter dans .env (voir INSTALL.md) ou relancer avec --api-key"
    if not dry:
        with open(path, "a", encoding="utf-8") as f:
            f.write(("" if existing.endswith("\n") or not existing else "\n") + f"FLOWISE_API_KEY={key}\n")
        os.chmod(path, 0o600)
    return "FLOWISE_API_KEY ajoutée à .env"


def run(args):
    home = os.path.abspath(os.path.expanduser(args.hermes_home))
    config_path, env_path = os.path.join(home, "config.yaml"), os.path.join(home, ".env")
    target_dir = os.path.join(home, "tools", "flowise-mcp")
    server_path = os.path.join(target_dir, "server.py")
    dry = args.dry_run
    log = lambda msg: print(("[simulation] " if dry else "") + msg)

    if not os.path.isdir(home):
        print(f"Erreur : dossier Hermes introuvable : {home} (option --hermes-home).", file=sys.stderr)
        return 2
    text = open(config_path, encoding="utf-8").read() if os.path.exists(config_path) else ""

    if args.uninstall:
        new_text, removed = remove_block(text)
        if removed:
            error = validate(new_text)
            if error:
                print(f"Erreur : {error} ; rien n'a été modifié.", file=sys.stderr)
                return 1
            if not dry:
                shutil.copy2(config_path, f"{config_path}.bak-flowise-{int(time.time())}")
                open(config_path, "w", encoding="utf-8").write(new_text)
        log("entrée mcp_servers.flowise retirée de config.yaml" if removed else "entrée flowise absente de config.yaml")
        if os.path.isdir(target_dir):
            if not dry:
                shutil.rmtree(target_dir)
            log(f"dossier supprimé : {target_dir}")
        print("La clé FLOWISE_API_KEY de .env n'est pas supprimée. Recharger les outils (/reload-mcp) pour finir.")
        return 0

    log(f"copie de server.py -> {server_path}")
    if not dry:
        os.makedirs(target_dir, exist_ok=True)
        shutil.copy2(os.path.join(HERE, "server.py"), server_path)
        os.chmod(server_path, 0o755)

    new_text = add_block(text, server_block(server_path, args.flowise_url, args.allow, args.python))
    error = validate(new_text)
    if error:
        print(f"Erreur : {error} ; config.yaml n'a pas été modifié.", file=sys.stderr)
        return 1
    if new_text != text:
        if not dry:
            if os.path.exists(config_path):
                backup = f"{config_path}.bak-flowise-{int(time.time())}"
                shutil.copy2(config_path, backup)
                log(f"sauvegarde : {backup}")
            open(config_path, "w", encoding="utf-8").write(new_text)
        log("serveur « flowise » déclaré dans config.yaml (mcp_servers)")
    else:
        log("config.yaml déjà à jour")

    log(update_env_file(env_path, args.api_key or os.environ.get("FLOWISE_API_KEY", ""), dry))

    if dry:
        return 0
    env = {**os.environ, "FLOWISE_URL": args.flowise_url, "FLOWISE_MCP_ALLOW": args.allow}
    if not env.get("FLOWISE_API_KEY") and os.path.exists(env_path):
        m = re.search(r"^FLOWISE_API_KEY=(.+)$", open(env_path, encoding="utf-8").read(), re.M)
        if m:
            env["FLOWISE_API_KEY"] = m.group(1).strip().strip("\"'")
    check = subprocess.run([sys.executable, server_path, "--selftest"], capture_output=True, text=True, env=env)
    print(check.stdout.strip())
    print("\nSuite : recharger les outils de Hermes (envoyer /reload-mcp à l'agent) ou redémarrer la passerelle, "
          "puis demander : « liste les flows Flowise disponibles ».")
    return 0 if check.returncode == 0 else 3


def main(argv=None):
    p = argparse.ArgumentParser(description="Installe l'outil Flowise (serveur MCP) dans Hermes.")
    p.add_argument("--hermes-home", default=default_home(), help="dossier de données de Hermes (défaut : $HERMES_HOME ou ~/.hermes)")
    p.add_argument("--flowise-url", default=os.environ.get("FLOWISE_URL", "http://localhost:3000"))
    p.add_argument("--api-key", help="clé API Flowise (sinon variable FLOWISE_API_KEY)")
    p.add_argument("--allow", default="mcp-*", help="motifs de noms de flows exposés à l'agent, séparés par des virgules (défaut : mcp-*)")
    p.add_argument("--python", default="python3", help="interpréteur lancé par Hermes pour server.py")
    p.add_argument("--dry-run", action="store_true", help="affiche ce qui serait fait sans rien écrire")
    p.add_argument("--uninstall", action="store_true", help="retire l'outil (config.yaml et dossier tools/flowise-mcp)")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
