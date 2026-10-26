import datetime
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
import server  # noqa: E402


def flow(flow_id, name, nodes=(), type_="AGENTFLOW"):
    """nodes : noms de nœuds, ou dicts « data » complets (pour tester les entrées et les notes)."""
    def data(n):
        return {"name": n} if isinstance(n, str) else dict(n)

    return {"id": flow_id, "name": name, "type": type_, "flowData": json.dumps({"nodes": [{"data": data(n)} for n in nodes], "edges": []})}


def execution(*nodes, state="ERROR", created=None):
    """Enregistrement d'exécution Flowise : (nodeId, nodeLabel, status, error)."""
    created = created or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {"id": "exec-1", "state": state, "createdDate": created,
            "executionData": json.dumps([{"nodeId": i, "nodeLabel": l, "status": s, "data": {"error": e}} for i, l, s, e in nodes])}


FLOWS = [
    flow("id-digest", "mcp-actu-ia", ["startAgentflow", "hermesTaskAgentflow", "directReplyAgentflow"]),
    flow("id-echo", "mcp-echo", ["startAgentflow", "directReplyAgentflow"]),
    flow("id-jobs", "mcp-gestion", ["hermesScheduleAgentflow"]),
    flow("id-prive", "surveillance bitcoins", ["hermesTaskAgentflow"]),
    flow("id-rapport", "mcp-rapport", [
        {"name": "startAgentflow", "inputs": {"startInputType": "formInput",
                                             "formInputTypes": [{"type": "string", "name": "sujet"}, {"type": "number", "name": "nbPages"}]}},
        {"name": "httpAgentflow", "inputs": {}},
        {"name": "directReplyAgentflow", "inputs": {}},
        {"name": "stickyNoteAgentflow", "inputs": {"note": "Rapport de veille hebdomadaire\n\nDeuxième ligne, ignorée."}},
    ]),
]


class FakeFlowise(BaseHTTPRequestHandler):
    calls = []
    behavior = {}
    executions = []

    def log_message(self, *a):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.headers.get("Authorization") == "Bearer secret"

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {"error": "Unauthorized"})
        if self.path == "/api/v1/chatflows":
            return self.reply(200, FLOWS)
        if self.path.startswith("/api/v1/executions"):
            return self.reply(200, {"data": FakeFlowise.executions, "total": len(FakeFlowise.executions)})
        self.reply(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeFlowise.calls.append((self.path, body, dict(self.headers)))
        if not self.authorized():
            return self.reply(401, {"error": "Unauthorized"})
        forced = FakeFlowise.behavior.get(self.path)
        if forced:
            return self.reply(*forced)
        self.reply(200, {"text": f"réponse à : {body['question']}"})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), FakeFlowise)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeFlowise.calls.clear()
        FakeFlowise.behavior.clear()
        FakeFlowise.executions = []
        server._slots = None

    def cfg(self, **env):
        base = {"FLOWISE_URL": self.url, "FLOWISE_API_KEY": "secret"}
        base.update(env)
        return server.load_config(base)

    def call(self, name, args, cfg=None):
        out = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, cfg or self.cfg())
        return out["result"]["isError"], out["result"]["content"][0]["text"]


class ConfigTest(unittest.TestCase):
    def test_defauts(self):
        c = server.load_config({})
        self.assertEqual((c["url"], c["allow"], c["allow_schedule"], c["max_concurrent"]), ("http://localhost:3000", ["mcp-*"], False, 2))

    def test_valeurs(self):
        c = server.load_config({"FLOWISE_URL": "http://f:3000/", "FLOWISE_MCP_ALLOW": "a*, b ,", "FLOWISE_MCP_ALLOW_SCHEDULE": "1", "FLOWISE_MCP_TIMEOUT": "12"})
        self.assertEqual((c["url"], c["allow"], c["allow_schedule"], c["timeout"]), ("http://f:3000", ["a*", "b"], True, 12.0))


class ToolsTest(Base):
    def test_liste_n_expose_que_les_flows_autorises(self):
        err, text = self.call("flowise_list_flows", {})
        self.assertFalse(err)
        self.assertIn("mcp-actu-ia", text)
        self.assertIn("mcp-echo", text)
        self.assertNotIn("surveillance bitcoins", text)   # hors du motif mcp-*
        self.assertNotIn("mcp-gestion", text)             # gère des jobs : refusé par défaut
        self.assertIn("risque de boucle", text.split("mcp-actu-ia")[1].split("\n")[0])  # signale l'appel à Hermes
        self.assertNotIn("risque de boucle", text.split("mcp-echo")[1].split("\n")[0])

    def test_liste_vide_explique_quoi_faire(self):
        _, text = self.call("flowise_list_flows", {}, self.cfg(FLOWISE_MCP_ALLOW="rien-*"))
        self.assertIn("Aucun flow autorisé", text)
        self.assertIn("rien-*", text)

    def test_allow_etoile_expose_tout_sauf_les_jobs(self):
        _, text = self.call("flowise_list_flows", {}, self.cfg(FLOWISE_MCP_ALLOW="*"))
        self.assertIn("surveillance bitcoins", text)
        self.assertNotIn("mcp-gestion", text)
        _, text = self.call("flowise_list_flows", {}, self.cfg(FLOWISE_MCP_ALLOW="*", FLOWISE_MCP_ALLOW_SCHEDULE="1"))
        self.assertIn("mcp-gestion", text)

    def test_allow_par_id(self):
        _, text = self.call("flowise_list_flows", {}, self.cfg(FLOWISE_MCP_ALLOW="id-prive"))
        self.assertIn("surveillance bitcoins", text)

    def test_liste_montre_contrat_entree_et_documentation(self):
        _, text = self.call("flowise_list_flows", {})
        rapport = text.split("mcp-rapport")[1].split("\n")[0]
        self.assertIn("entrée : formulaire (sujet, nbPages)", rapport)      # déduit du nœud Start
        self.assertIn("doc : « Rapport de veille hebdomadaire »", rapport)  # première ligne de la note
        self.assertIn("3 nœuds", rapport)                                   # la note autocollante ne compte pas
        self.assertIn("entrée : texte libre", text.split("mcp-echo")[1].split("\n")[0])

    def test_contrat_entree_par_defaut_et_flow_sans_start(self):
        self.assertEqual(server.input_contract([{"data": {"name": "startAgentflow", "inputs": {}}}]), "texte libre")
        self.assertEqual(server.input_contract([{"data": {"name": "startAgentflow", "inputs": {"startInputType": "webhookTrigger"}}}]), "webhook")
        self.assertIsNone(server.input_contract([{"data": {"name": "directReplyAgentflow"}}]))
        self.assertIsNone(server.summary([{"data": {"name": "stickyNoteAgentflow", "inputs": {"note": "\n\n"}}}]))

    def test_run_par_nom_et_par_id(self):
        for ref in ("mcp-echo", "MCP-ECHO", "id-echo"):
            with self.subTest(ref=ref):
                err, text = self.call("flowise_run_flow", {"flow": ref, "question": "bonjour"})
                self.assertEqual((err, text), (False, "réponse à : bonjour"))
        path, body, headers = FakeFlowise.calls[0]
        self.assertEqual(path, "/api/v1/prediction/id-echo")
        self.assertEqual(body, {"question": "bonjour", "streaming": False})
        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_session_id_transmis(self):
        self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "q", "session_id": "s1"})
        self.assertEqual(FakeFlowise.calls[0][1]["overrideConfig"], {"sessionId": "s1"})

    def test_refus_flow_non_autorise_sans_appel(self):
        for ref, pattern in (("surveillance bitcoins", "non autorisé.*FLOWISE_MCP_ALLOW"), ("mcp-gestion", "Hermes Schedule")):
            with self.subTest(ref=ref):
                err, text = self.call("flowise_run_flow", {"flow": ref, "question": "x"})
                self.assertTrue(err)
                self.assertRegex(text, pattern)
        self.assertEqual(FakeFlowise.calls, [])

    def test_erreurs_de_parametres_et_flow_inconnu(self):
        self.assertRegex(self.call("flowise_run_flow", {"question": "x"})[1], "« flow » manquant")
        self.assertRegex(self.call("flowise_run_flow", {"flow": "mcp-echo"})[1], "« question » manquant")
        self.assertRegex(self.call("flowise_run_flow", {"flow": "nimporte", "question": "x"})[1], "Aucun flow « nimporte »")

    def test_cle_refusee(self):
        err, text = self.call("flowise_list_flows", {}, self.cfg(FLOWISE_API_KEY="mauvaise"))
        self.assertTrue(err)
        self.assertRegex(text, "HTTP 401.*FLOWISE_API_KEY")

    def test_flowise_injoignable(self):
        err, text = self.call("flowise_list_flows", {}, self.cfg(FLOWISE_URL="http://127.0.0.1:9"))
        self.assertTrue(err)
        self.assertRegex(text, "injoignable.*FLOWISE_URL")

    def test_erreur_http_du_flow_remontee(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "Hermes Task : Hermes est injoignable"})
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertTrue(err)
        self.assertRegex(text, "HTTP 500.*Hermes est injoignable")

    def test_reponse_sans_texte_et_texte_vide(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (200, {"json": {"a": 1}})
        self.assertIn('"a": 1', self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})[1])
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (200, {"text": "  "})
        self.assertIn("vide", self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})[1])

    def test_limite_d_appels_simultanes(self):
        cfg = self.cfg(FLOWISE_MCP_MAX_CONCURRENT="1")
        server._slots = server.threading.BoundedSemaphore(1)
        server._slots.acquire()  # un appel déjà en cours
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"}, cfg)
        self.assertTrue(err)
        self.assertIn("Trop d'appels", text)


class DiagnosisTest(Base):
    """Un échec doit nommer le nœud fautif, lu dans la dernière exécution enregistrée par Flowise."""

    def test_echec_nomme_le_noeud_fautif(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "buildChatflow - NodeVM Execution Error"})
        FakeFlowise.executions = [execution(
            ("startAgentflow_0", "Start", "FINISHED", None),
            ("customFunctionAgentflow_0", "Analyser", "ERROR", "Error: NodeVM Execution Error: Error: BOOM-TEST"))]
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertTrue(err)
        self.assertRegex(text, "HTTP 500")  # l'erreur d'origine reste en tête
        self.assertIn("nœud « Analyser » (customFunctionAgentflow_0)", text)
        self.assertIn("BOOM-TEST", text)
        self.assertIn("Dernier nœud réussi : startAgentflow_0", text)

    def test_echec_sans_trace_garde_le_message_d_origine(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "Boom"})
        FakeFlowise.executions = []  # Flowise n'a rien enregistré
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertTrue(err)
        self.assertRegex(text, "HTTP 500.*Boom")
        self.assertNotIn("Trace Flowise", text)

    def test_diagnostic_sans_noeud_en_erreur_n_est_pas_ajoute(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "Boom"})
        FakeFlowise.executions = [execution(("startAgentflow_0", "Start", "FINISHED", None), state="FINISHED")]
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertTrue(err)
        self.assertNotIn("Trace Flowise", text)

    def test_trace_illisible_ou_api_absente_ne_casse_rien(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "Boom"})
        FakeFlowise.executions = [{"id": "x", "state": "ERROR", "createdDate": "hier", "executionData": "pas du JSON"}]
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertTrue(err)
        self.assertRegex(text, "HTTP 500.*Boom")

    def test_trace_trop_ancienne_repliee_sur_la_plus_recente(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "Boom"})
        FakeFlowise.executions = [execution(("vieux_0", "Vieux", "ERROR", "erreur ancienne"), created="2020-01-01T00:00:00.000Z")]
        _, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertIn("Vieux", text)  # faute de mieux, on lit la plus récente disponible

    def test_verbose_ajoute_la_trace_des_noeuds(self):
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (200, {"text": "ok", "agentFlowExecutedData": [
            {"nodeId": "startAgentflow_0", "nodeLabel": "Start", "status": "FINISHED"},
            {"nodeId": "directReplyAgentflow_0", "nodeLabel": "Réponse", "status": "FINISHED"}]})
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x", "verbose": True})
        self.assertFalse(err)
        self.assertTrue(text.startswith("ok"))
        self.assertIn("Trace des nœuds", text)
        self.assertIn("directReplyAgentflow_0 (Réponse) : FINISHED", text)
        _, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertNotIn("Trace des nœuds", text)  # silencieux par défaut


class ProtocolTest(unittest.TestCase):
    def test_initialize_et_liste_des_outils(self):
        cfg = server.load_config({})
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}, cfg)["result"]
        self.assertEqual((init["protocolVersion"], init["serverInfo"]["name"], "tools" in init["capabilities"]), ("2024-11-05", "flowise", True))
        init2 = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "9999-01-01"}}, cfg)["result"]
        self.assertEqual(init2["protocolVersion"], server.SUPPORTED_PROTOCOLS[0])
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, cfg)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["flowise_list_flows", "flowise_run_flow"])
        self.assertEqual(tools[1]["inputSchema"]["required"], ["flow", "question"])
        self.assertIn("verbose", tools[1]["inputSchema"]["properties"])

    def test_notification_sans_reponse_methode_et_outil_inconnus(self):
        cfg = server.load_config({})
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, cfg))
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"}, cfg)["result"], {})
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 4, "method": "foo"}, cfg)["error"]["code"], -32601)
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "x"}}, cfg)["error"]["code"], -32602)


class StdioEndToEnd(Base):
    """Lance server.py comme le fait Hermes (processus, stdio) contre le faux Flowise."""

    def test_session_complete(self):
        env = {**os.environ, "FLOWISE_URL": self.url, "FLOWISE_API_KEY": "secret"}
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "flowise_run_flow", "arguments": {"flow": "mcp-echo", "question": "salut"}}},
        ]
        out, err = proc.communicate("\n".join(json.dumps(m) for m in messages) + "\n", timeout=30)
        replies = {r["id"]: r for r in map(json.loads, out.strip().splitlines())}
        self.assertEqual(set(replies), {1, 2, 3})  # la notification n'a pas de réponse
        self.assertEqual(replies[3]["result"]["content"][0]["text"], "réponse à : salut")
        self.assertEqual(proc.returncode, 0, err)

    def test_selftest(self):
        env = {**os.environ, "FLOWISE_URL": self.url, "FLOWISE_API_KEY": "secret"}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "--selftest"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("3 exposé(s)", r.stdout)
        self.assertNotIn("secret", r.stdout)  # la clé n'est jamais affichée
        bad = subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "--selftest"], capture_output=True, text=True, env={**env, "FLOWISE_API_KEY": "x"}, timeout=30)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ÉCHEC", bad.stdout)


if __name__ == "__main__":
    unittest.main()
