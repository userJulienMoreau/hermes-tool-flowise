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
    return {"id": flow_id, "name": name, "type": type_, "flowData": json.dumps({"nodes": [{"data": {"name": n}} for n in nodes], "edges": []})}


FLOWS = [
    flow("id-digest", "mcp-actu-ia", ["startAgentflow", "hermesTaskAgentflow", "directReplyAgentflow"]),
    flow("id-echo", "mcp-echo", ["startAgentflow", "directReplyAgentflow"]),
    flow("id-jobs", "mcp-gestion", ["hermesScheduleAgentflow"]),
    flow("id-prive", "surveillance bitcoins", ["hermesTaskAgentflow"]),
]


class FakeFlowise(BaseHTTPRequestHandler):
    calls = []
    behavior = {}

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
        self.assertIn("2 exposé(s)", r.stdout)
        self.assertNotIn("secret", r.stdout)  # la clé n'est jamais affichée
        bad = subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "--selftest"], capture_output=True, text=True, env={**env, "FLOWISE_API_KEY": "x"}, timeout=30)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ÉCHEC", bad.stdout)


if __name__ == "__main__":
    unittest.main()
