# hermes-tool-flowise — l'outil Flowise pour Hermes Agent

Donne à l'agent [Hermes](https://hermes-agent.nousresearch.com/docs) deux outils pour utiliser vos flows [Flowise](https://docs.flowiseai.com) :

| Outil (nom côté Hermes) | Rôle |
|---|---|
| `mcp__flowise__flowise_list_flows` | liste les flows que l'agent a le droit d'exécuter |
| `mcp__flowise__flowise_run_flow` | exécute un flow (`flow` = nom ou id, `question` = entrée) et renvoie sa réponse |

C'est un **serveur MCP** (stdio) en un seul fichier Python, **sans dépendance** : `server.py`. L'installeur `install.py` le déclare dans
la configuration de Hermes (`mcp_servers:` de `config.yaml`, documenté dans la [référence MCP de Hermes](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference)).

## Installation (≈ 1 minute)

Prérequis : un Flowise joignable et une clé API (Flowise → *API Keys*).

```bash
git clone https://github.com/userJulienMoreau/hermes-tool-flowise.git
cd hermes-tool-flowise
FLOWISE_API_KEY=<votre clé> python3 install.py --flowise-url http://localhost:3000
```

Puis recharger les outils de Hermes (`/reload-mcp`) ou redémarrer sa passerelle. Valeurs par défaut : dossier de données `$HERMES_HOME` ou `~/.hermes`.

**Prérequis, et seulement ceux-là :** Python 3 (bibliothèque standard, rien à installer), un Hermes, un Flowise joignable et une clé API. Aucun autre projet ni
Docker Compose particulier n'est nécessaire.

**Si Hermes tourne dans un conteneur Docker**, l'installeur doit s'exécuter *dans* ce conteneur, avec le dossier de données de Hermes et l'URL de Flowise
vus depuis lui. Remplacez les trois valeurs entre chevrons par celles de votre installation :

```bash
docker cp . <conteneur-hermes>:/tmp/hft                  # depuis le dossier cloné
docker exec <conteneur-hermes> python3 /tmp/hft/install.py \
    --hermes-home <dossier-de-données-de-Hermes> --flowise-url <URL de Flowise vue depuis le conteneur>
docker restart <conteneur-hermes>
```
Exemple avec le projet [hermes-flowise](https://github.com/userJulienMoreau/hermes-flowise) (facultatif) : conteneur `hermes`, dossier `/opt/data`, Flowise sur `http://flowise:3000`.
Depuis un conteneur, `localhost` désigne le conteneur lui-même : utilisez le nom du service ou l'adresse de l'hôte (`host.docker.internal`).

**Faire installer l'outil par Hermes lui-même :** lui envoyer
« Installe l'outil Flowise : https://github.com/userJulienMoreau/hermes-tool-flowise — suis son `INSTALL.md` ».
[INSTALL.md](INSTALL.md) est écrit pour l'agent (étapes, vérifications, règles de prudence). Un dépôt **privé** exige que l'agent dispose d'un accès
(jeton GitHub) ; sinon le rendre public ou copier le dossier à la main.

Options de `install.py` : `--hermes-home`, `--flowise-url`, `--api-key`, `--allow`, `--python`, `--dry-run` (aucune écriture), `--uninstall`.
L'installeur est idempotent, sauvegarde `config.yaml` (`config.yaml.bak-flowise-<date>`), valide le YAML avant d'écrire et n'affiche jamais la clé
(elle va dans `.env`, `config.yaml` ne contient que `${FLOWISE_API_KEY}`).

## Quels flows l'agent peut-il exécuter ?

**Par défaut, seulement ceux dont le nom commence par `mcp-`** (ex. `mcp-actu-ia`) : renommer un flow suffit pour l'exposer, ou le renommer pour le retirer.

| Réglage (`env:` du serveur dans `config.yaml`) | Effet |
|---|---|
| `FLOWISE_MCP_ALLOW` | motifs de noms séparés par des virgules (`mcp-*`, `Actu IA`, `*` = tous) ; un id exact fonctionne aussi |
| `FLOWISE_MCP_ALLOW_SCHEDULE=1` | autorise aussi les flows contenant un nœud **Hermes Schedule** (qui peut créer, suspendre ou supprimer des jobs) ; refusés par défaut |
| `FLOWISE_MCP_TIMEOUT` | délai d'un flow en secondes (défaut 300) |
| `FLOWISE_MCP_MAX_CONCURRENT` | appels de flows simultanés (défaut 2) |

**Boucle possible :** un flow qui contient un nœud *Hermes Task* rappelle Hermes. `flowise_list_flows` le signale
(« appelle Hermes : ne pas le rappeler depuis ce flow ») ; ne donnez pas à un tel flow le droit de rappeler cet outil.

## Vérification et dépannage

```bash
FLOWISE_URL=http://localhost:3000 FLOWISE_API_KEY=... python3 ~/.hermes/tools/flowise-mcp/server.py --selftest
```

| Symptôme | Cause / remède |
|---|---|
| « Aucun flow autorisé » | aucun nom ne correspond à `FLOWISE_MCP_ALLOW` : renommer un flow en `mcp-…` |
| « Flowise a refusé l'accès (HTTP 401) » | `FLOWISE_API_KEY` absente ou invalide (`.env` de Hermes) |
| « Flowise injoignable » | `FLOWISE_URL` : depuis un conteneur Hermes, utiliser le nom de service (`http://flowise:3000`), pas `localhost` |
| Outils absents après l'installation | `/reload-mcp` ou redémarrage de la passerelle ; vérifier `mcp_servers.flowise` dans `config.yaml` |
| Un flow lent | ajuster `FLOWISE_MCP_TIMEOUT` et `timeout:` du serveur (secondes) |

Désinstaller : `python3 install.py --uninstall` (retire l'entrée de `config.yaml` et `tools/flowise-mcp/`, conserve la clé de `.env`).

## Développement

```bash
python3 -I -m unittest discover -s tests     # 35 tests : protocole stdio, outils, filtre, installeur
```
Le serveur parle JSON-RPC 2.0 (un message par ligne) et accepte les versions de protocole MCP 2025-06-18, 2025-03-26 et 2024-11-05. Les tests
lancent `server.py` comme le fait Hermes (processus + stdio) contre un faux Flowise HTTP.
