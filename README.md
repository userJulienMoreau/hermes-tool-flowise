# hermes-tool-flowise — l'outil Flowise pour Hermes Agent

Donne à l'agent [Hermes](https://hermes-agent.nousresearch.com/docs) deux outils pour utiliser vos flows [Flowise](https://docs.flowiseai.com) :

| Outil (nom côté Hermes) | Rôle |
|---|---|
| `mcp__flowise__flowise_list_flows` | liste les flows que l'agent a le droit d'exécuter, avec pour chacun l'entrée attendue, sa documentation et son nombre de nœuds |
| `mcp__flowise__flowise_run_flow` | exécute un flow (`flow` = nom ou id, `question` = entrée, `session_id`, `verbose`) et renvoie sa réponse ; en cas d'échec, **nomme le nœud fautif** |

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

## Savoir ce que l'agent appelle

L'agent ne voyait que le nom, l'id et le type : il devait deviner si la `question` attendue était un sujet, un
critère ou du JSON. `flowise_list_flows` décrit maintenant chaque flow à partir de son propre graphe :

```
- mcp-exemple-veille-crypto (id 08765dba-…, AGENTFLOW) · entrée : texte libre · 10 nœuds · doc : « EXEMPLE — veille de marché à seuil » · [appelle Hermes : …]
- mcp-rapport (id …, AGENTFLOW) · entrée : formulaire (sujet, nbPages) · 3 nœuds · doc : « Rapport de veille hebdomadaire »
```

- **Entrée attendue** : déduite du nœud *Start* — `texte libre` (Chat Input), `formulaire` suivi du nom de ses champs
  (Form Input), `webhook`, ou `planifié`.
- **Documentation** : la **première ligne non vide de la première note autocollante** (Sticky Note) du flow.
  Convention simple et sans configuration : posez une note en tête de flow, elle devient sa description.
  Un flow sans note n'affiche simplement pas de `doc :`.
- **Nombre de nœuds** : les notes autocollantes ne sont pas comptées.

## Diagnostiquer un échec

Quand un nœud échoue, la réponse de `/api/v1/prediction` **ne dit pas lequel** : Flowise renvoie un HTTP 500 avec
un message brut (`Cannot read properties of undefined (reading 'trim')`). Le serveur relit donc la dernière
exécution enregistrée par Flowise (`GET /api/v1/executions?chatflowId=…`) et complète l'erreur :

```
Erreur : Flowise a répondu HTTP 500. {"message":"Error: predictionsServices.buildChatflow - Error: NodeVM Execution Error: Error: BOOM-TEST"}
Trace Flowise : nœud « cf qui plante » (customFunctionAgentflow_0) : Error: NodeVM Execution Error: Error: BOOM-TEST
Dernier nœud réussi : startAgentflow_0
```

L'erreur d'origine est toujours conservée en tête ; le diagnostic vient en complément. Si l'API des exécutions
n'est pas disponible (version ancienne de Flowise, droits, panne) ou si la trace est illisible, le message
d'origine est renvoyé inchangé — jamais d'échec à cause du diagnostic.

Sur une exécution **réussie**, `verbose=true` ajoute la trace complète, utile pour comprendre un résultat inattendu :

```
NO_ALERT

Trace des nœuds :
  - startAgentflow_0 (Start) : FINISHED
  - customFunctionAgentflow_0 (1. Préparer le critère) : FINISHED
  - httpAgentflow_0 (2. Interroger CoinGecko) : FINISHED
  - hermesTaskAgentflow_0 (5. Verdict Hermes) : FINISHED
  - directReplyAgentflow_1 (7b. Silence (NO_ALERT)) : FINISHED
```

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
| L'erreur ne nomme pas le nœud | aucune exécution exploitable côté Flowise : lancer le flow une fois depuis son interface, puis réessayer |

Désinstaller : `python3 install.py --uninstall` (retire l'entrée de `config.yaml` et `tools/flowise-mcp/`, conserve la clé de `.env`).

## Développement

```bash
python3 -I -m unittest discover -s tests     # 43 tests : protocole stdio, outils, filtre, diagnostic, installeur
```
Le serveur parle JSON-RPC 2.0 (un message par ligne) et accepte les versions de protocole MCP 2025-06-18, 2025-03-26 et 2024-11-05. Les tests
lancent `server.py` comme le fait Hermes (processus + stdio) contre un faux Flowise HTTP.

## Licence

[MIT](LICENSE) — usage, copie, modification et redistribution libres, sans garantie.
