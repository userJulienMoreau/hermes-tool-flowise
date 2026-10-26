import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
import install  # noqa: E402

_, yaml = install.load_yaml("a: 1")  # vrai si PyYAML ou ruamel.yaml est disponible ; sinon on vérifie le texte seulement

BLOCK = install.server_block("/h/tools/flowise-mcp/server.py", "http://flowise:3000", "mcp-*", "python3")
SAMPLE = "# configuration Hermes\nmodel:\n  default: x\n\n# mcp_servers: exemple commenté\nterminal:\n  backend: local\n"


def parsed(text):
    return install.load_yaml(text)[0] if yaml else None


class ConfigEditTest(unittest.TestCase):
    def test_ajout_quand_mcp_servers_est_absent(self):
        out = install.add_block(SAMPLE, BLOCK)
        self.assertTrue(out.startswith(SAMPLE.rstrip("\n")))          # le contenu et les commentaires existants sont intacts
        self.assertIn("mcp_servers:\n  flowise:\n    command: python3", out)
        if yaml:
            entry = parsed(out)["mcp_servers"]["flowise"]
            self.assertEqual(entry["args"], ["/h/tools/flowise-mcp/server.py"])
            self.assertEqual(entry["env"]["FLOWISE_API_KEY"], "${FLOWISE_API_KEY}")
            self.assertEqual(entry["tools"]["include"], list(install.TOOLS))
            self.assertEqual(parsed(out)["model"], {"default": "x"})

    def test_ajout_a_un_fichier_vide(self):
        out = install.add_block("", BLOCK)
        self.assertTrue(out.startswith("mcp_servers:\n  flowise:"))

    def test_ajout_a_cote_d_un_autre_serveur(self):
        text = "model: x\nmcp_servers:\n  github:\n    command: npx\n    args: [\"-y\", \"gh\"]\nterminal:\n  backend: local\n"
        out = install.add_block(text, BLOCK)
        if yaml:
            servers = parsed(out)["mcp_servers"]
            self.assertEqual(set(servers), {"github", "flowise"})
            self.assertEqual(servers["github"], {"command": "npx", "args": ["-y", "gh"]})
            self.assertEqual(parsed(out)["terminal"], {"backend": "local"})
        self.assertIn("  github:\n    command: npx", out)

    def test_mcp_servers_vide(self):
        for empty in ("{}", "null", "~"):
            out = install.add_block(f"a: 1\nmcp_servers: {empty}\nb: 2\n", BLOCK)
            if yaml:
                self.assertEqual(list(parsed(out)["mcp_servers"]), ["flowise"])
                self.assertEqual((parsed(out)["a"], parsed(out)["b"]), (1, 2))

    def test_forme_inattendue_refusee(self):
        with self.assertRaises(ValueError):
            install.add_block("mcp_servers: [a, b]\n", BLOCK)

    def test_idempotence_et_mise_a_jour(self):
        once = install.add_block(SAMPLE, BLOCK)
        self.assertEqual(install.add_block(once, BLOCK), once)
        newer = install.server_block("/h/tools/flowise-mcp/server.py", "http://autre:3000", "*", "python3")
        updated = install.add_block(once, newer)
        self.assertEqual(updated.count("flowise:"), 1)
        self.assertIn("http://autre:3000", updated)
        self.assertNotIn("http://flowise:3000", updated)

    def test_retrait_conserve_le_reste(self):
        text = "model: x\nmcp_servers:\n  github:\n    command: npx\nterminal:\n  backend: local\n"
        out, removed = install.remove_block(install.add_block(text, BLOCK))
        self.assertTrue(removed)
        self.assertEqual(out, text)

    def test_retrait_du_seul_serveur_laisse_une_cle_valide(self):
        out, removed = install.remove_block(install.add_block(SAMPLE, BLOCK))
        self.assertTrue(removed)
        self.assertIn("mcp_servers: {}", out)
        if yaml:
            self.assertEqual(parsed(out)["mcp_servers"], {})

    def test_retrait_sans_entree(self):
        out, removed = install.remove_block(SAMPLE)
        self.assertEqual((out, removed), (SAMPLE, False))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.cfg = os.path.join(self.home, "config.yaml")
        with open(self.cfg, "w") as f:
            f.write(SAMPLE)

    def run_install(self, *extra, env=None):
        full_env = {k: v for k, v in os.environ.items() if k != "FLOWISE_API_KEY"}
        full_env.update(env or {})
        return subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), "--hermes-home", self.home, "--flowise-url", "http://127.0.0.1:9", *extra],
                              capture_output=True, text=True, env=full_env, timeout=60)

    def read(self, name):
        return open(os.path.join(self.home, name)).read()

    def test_dry_run_n_ecrit_rien(self):
        r = self.run_install("--dry-run", "--api-key", "k")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[simulation]", r.stdout)
        self.assertEqual(self.read("config.yaml"), SAMPLE)
        self.assertFalse(os.path.exists(os.path.join(self.home, "tools")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".env")))

    def test_installation_complete(self):
        r = self.run_install("--api-key", "cle-secrete-123")
        self.assertIn("ÉCHEC", r.stdout)           # Flowise (port 9) injoignable : l'auto-test le signale
        self.assertEqual(r.returncode, 3)
        self.assertTrue(os.access(os.path.join(self.home, "tools", "flowise-mcp", "server.py"), os.X_OK))
        self.assertIn("mcp_servers:", self.read("config.yaml"))
        self.assertIn("FLOWISE_API_KEY=cle-secrete-123", self.read(".env"))
        self.assertEqual(oct(os.stat(os.path.join(self.home, ".env")).st_mode & 0o777), "0o600")
        self.assertNotIn("cle-secrete-123", r.stdout + r.stderr)       # la clé n'est jamais affichée
        self.assertNotIn("cle-secrete-123", self.read("config.yaml"))   # ni écrite dans config.yaml (référence ${VAR})
        self.assertTrue([f for f in os.listdir(self.home) if f.startswith("config.yaml.bak-flowise-")])

    def test_reinstallation_idempotente(self):
        self.run_install("--api-key", "k1")
        first = self.read("config.yaml")
        r = self.run_install("--api-key", "k2")
        self.assertEqual(self.read("config.yaml"), first)
        self.assertIn("déjà à jour", r.stdout)
        self.assertEqual(self.read(".env").count("FLOWISE_API_KEY="), 1)   # clé existante non dupliquée ni écrasée
        self.assertIn("k1", self.read(".env"))

    def test_cle_lue_dans_l_environnement(self):
        self.run_install(env={"FLOWISE_API_KEY": "depuis-env"})
        self.assertIn("FLOWISE_API_KEY=depuis-env", self.read(".env"))

    def test_desinstallation(self):
        self.run_install("--api-key", "k")
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("flowise:", self.read("config.yaml"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "tools", "flowise-mcp")))
        self.assertIn("FLOWISE_API_KEY=k", self.read(".env"))       # la clé n'est pas supprimée

    def test_dossier_hermes_introuvable(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), "--hermes-home", "/nexiste/pas"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("introuvable", r.stderr)

    @unittest.skipUnless(yaml, "aucune bibliothèque YAML")
    def test_retour_arriere_si_yaml_invalide(self):
        original = "model: [oups\n"          # fichier déjà invalide : aucune écriture ne doit avoir lieu
        with open(self.cfg, "w") as f:
            f.write(original)
        r = self.run_install("--api-key", "k")
        self.assertEqual(r.returncode, 1)
        self.assertIn("YAML invalide", r.stderr)
        self.assertEqual(self.read("config.yaml"), original)


if __name__ == "__main__":
    unittest.main()
