# Installation de l'outil Flowise — instructions pour l'agent Hermes

Tu lis ce fichier parce que l'utilisateur t'a demandé d'installer l'outil « Flowise » (serveur MCP qui exécute des flows Flowise).
Suis ces étapes dans l'ordre. **Ne jamais afficher ni répéter une clé API.** Arrête-toi et demande à l'utilisateur en cas de doute.

## 0. Ce qu'il te faut
- L'URL de Flowise : `http://flowise:3000` si tu tournes dans le conteneur Docker du projet `hermes-flowise`, sinon `http://localhost:3000`.
- Une **clé API Flowise** : si la variable d'environnement `FLOWISE_API_KEY` est vide, demande à l'utilisateur de la créer (Flowise → *API Keys* → *Add New*) et de
  la placer dans `.env` (ou de te la fournir par un canal sûr). Sans clé, ne continue pas.
- Ton dossier de données : `$HERMES_HOME` (dans le conteneur : `/opt/data` ; sinon `~/.hermes`).

## 1. Récupérer le dépôt
```bash
git clone https://github.com/userJulienMoreau/hermes-tool-flowise.git /tmp/hermes-tool-flowise
cd /tmp/hermes-tool-flowise
```
Si le clonage est refusé (dépôt privé), demande à l'utilisateur un accès ou de copier le dossier cloné.

## 2. Simuler, puis installer
```bash
python3 install.py --hermes-home "$HERMES_HOME" --flowise-url <URL de Flowise> --dry-run
python3 install.py --hermes-home "$HERMES_HOME" --flowise-url <URL de Flowise>
```
Résultat attendu : « serveur « flowise » déclaré dans config.yaml » puis une ligne `OK : N flow(s) dans Flowise, M exposé(s) à l'agent`.
Si la dernière ligne commence par `ÉCHEC`, lis le message (clé refusée, Flowise injoignable) et corrige avant de poursuivre.
Le script sauvegarde `config.yaml` avant d'y toucher ; en cas d'erreur il n'écrit rien. Ne modifie pas `config.yaml` à la main.

## 3. Activer
Demande à l'utilisateur d'envoyer `/reload-mcp` (ou redémarre la passerelle Hermes si tu peux le faire). Un redémarrage interrompt les conversations en cours : préviens-le.

## 4. Vérifier
Appelle l'outil `mcp__flowise__flowise_list_flows`.
- Il liste des flows : c'est installé. Ne lance aucun flow sans demande de l'utilisateur.
- « Aucun flow autorisé » : l'installation est correcte. Explique que seuls les flows dont le nom commence par `mcp-` sont exposés et propose de renommer un flow dans
  Flowise (ex. « Actu IA » → « mcp-actu-ia »).
- Outil introuvable : relis l'étape 3 et vérifie que `mcp_servers.flowise` existe dans `config.yaml`.

## Règles de prudence
- N'utilise `--allow '*'` ou `FLOWISE_MCP_ALLOW_SCHEDULE=1` que si l'utilisateur le demande explicitement (ils exposent tous les flows / ceux qui gèrent des jobs).
- Un flow qui contient un nœud Hermes Task te rappelle toi-même : ne l'exécute pas en boucle et ne le fais pas rappeler cet outil.
- Désinstallation : `python3 install.py --uninstall --hermes-home "$HERMES_HOME"`, puis `/reload-mcp`.
