"""Tests du diagnostic (`flowise_doctor`) : pannes silencieuses du planificateur et flows cassés.

Le diagnostic doit détecter ce qui casse SANS prévenir : un job en erreur que personne ne lit, un job
en pause oublié, un job en retard, un flow structurellement incapable de fonctionner.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import server  # noqa: E402
from test_server import Base, FLOWS, FakeFlowise, execution, flow  # noqa: E402


def healthy_job(**overrides):
    """Job Hermes sain : actif, dernier passage réussi, prochain passage dans une heure."""
    job = {"id": "j1", "name": "Veille crypto", "enabled": True, "state": "scheduled", "last_status": "ok",
           "next_run_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()}
    job.update(overrides)
    return job


def home_with(jobs):
    """Dossier HERMES_HOME temporaire contenant un cron/jobs.json."""
    directory = tempfile.mkdtemp()
    os.makedirs(os.path.join(directory, "cron"), exist_ok=True)
    with open(os.path.join(directory, "cron", "jobs.json"), "w", encoding="utf-8") as handle:
        json.dump({"jobs": jobs}, handle)
    return directory


class PreflightTest(unittest.TestCase):
    """Contrôles structurels : chaque règle correspond à une panne réellement rencontrée."""

    @staticmethod
    def nodes(*extra):
        return [{"data": {"name": "startAgentflow"}}, {"data": {"name": "directReplyAgentflow"}}] + list(extra)

    def test_flow_sain(self):
        self.assertEqual(server.preflight(self.nodes()), [])

    def test_identifiant_laisse_en_objet(self):
        """Le piège qui a coûté une heure : « credential » en objet au lieu de l'id -> HTTP 500 « reading 'trim' »."""
        problemes = server.preflight(self.nodes({"data": {"name": "hermesTaskAgentflow", "label": "Tâche", "credential": {"name": "credential"}}}))
        self.assertTrue(any("mal formé" in p for p in problemes))

    def test_identifiant_absent_sur_un_noeud_hermes(self):
        problemes = server.preflight(self.nodes({"data": {"name": "hermesTaskAgentflow", "label": "Tâche", "credential": ""}}))
        self.assertTrue(any("Hermes API" in p for p in problemes))

    def test_url_et_fonction_vides(self):
        problemes = server.preflight(self.nodes(
            {"data": {"name": "httpAgentflow", "label": "HTTP", "inputs": {"url": "   "}}},
            {"data": {"name": "customFunctionAgentflow", "label": "CF", "inputs": {}}}))
        self.assertTrue(any("URL vide" in p for p in problemes))
        self.assertTrue(any("JavaScript vide" in p for p in problemes))

    def test_tache_hermes_vide(self):
        problemes = server.preflight(self.nodes({"data": {"name": "hermesTaskAgentflow", "label": "Tâche", "inputs": {"hermesPrompt": " "}}}))
        self.assertTrue(any("tâche vide" in p for p in problemes))

    def test_sans_start_ni_reponse(self):
        problemes = server.preflight([{"data": {"name": "llmAgentflow"}}])
        self.assertTrue(any("aucun nœud Start" in p for p in problemes))
        self.assertTrue(any("Direct Reply" in p for p in problemes))

    def test_un_noeud_a_sortie_cache_compte_comme_reponse(self):
        self.assertEqual(server.preflight([{"data": {"name": "startAgentflow"}}, {"data": {"name": "x", "hideOutput": True}}]), [])


class JobIssuesTest(unittest.TestCase):
    """Le cœur du diagnostic : un job qui ne tourne pas ne produit aucun message, donc rien ne se voit."""

    def test_job_sain_ne_produit_rien(self):
        self.assertEqual(server.job_issues([healthy_job()], now=time.time()), [])

    def test_job_en_pause(self):
        issues = server.job_issues([healthy_job(enabled=False, state="paused", paused_at="2026-10-26T21:22:00Z")])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "ATTENTION")
        self.assertIn("en pause", issues[0][1])
        self.assertIn("2026-10-26T21:22:00Z", issues[0][1])

    def test_job_en_erreur(self):
        issues = server.job_issues([healthy_job(last_status="error", last_error="Script exited with code 1 — timeout Flowise")])
        self.assertEqual(issues[0][0], "ÉCHEC")
        self.assertIn("timeout Flowise", issues[0][1])

    def test_job_avec_echecs_consecutifs(self):
        issues = server.job_issues([healthy_job(failure_streak=3, last_error="boum")])
        self.assertEqual(issues[0][0], "ÉCHEC")
        self.assertIn("3 échec", issues[0][1])

    def test_job_en_retard(self):
        vieux = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)).isoformat()
        issues = server.job_issues([healthy_job(next_run_at=vieux)])
        self.assertEqual(issues[0][0], "ÉCHEC")
        self.assertIn("en retard", issues[0][1])

    def test_job_a_intervalle_court_tolere_moins_de_retard(self):
        """4 h de retard : signalé pour un job horaire, toléré pour un job quotidien."""
        il_y_a_4h = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=4)).isoformat()
        horaire = healthy_job(schedule={"kind": "interval", "minutes": 60}, next_run_at=il_y_a_4h)
        quotidien = healthy_job(schedule={"kind": "interval", "minutes": 1440}, next_run_at=il_y_a_4h)
        self.assertTrue(server.job_issues([horaire]))
        self.assertEqual(server.job_issues([quotidien]), [])

    def test_job_ignore_est_passe_sous_silence(self):
        self.assertEqual(server.job_issues([healthy_job(enabled=False, state="paused")], ignore=["veille crypto"]), [])

    def test_human_delta(self):
        self.assertEqual(server.human_delta(45), "45 s")
        self.assertEqual(server.human_delta(600), "10 min")
        self.assertEqual(server.human_delta(3 * 3600 + 5 * 60), "3 h 05")
        self.assertEqual(server.human_delta(4 * 86400), "4 j")

    def test_parse_date(self):
        self.assertIsNotNone(server.parse_date("2026-10-26T22:05:11.000Z"))
        self.assertIsNotNone(server.parse_date("2026-10-26T20:46:37.130258+00:00"))
        self.assertIsNone(server.parse_date("hier"))
        self.assertIsNone(server.parse_date(None))


class DoctorTest(Base):
    """Le diagnostic complet, en lecture seule (aucun flow exécuté)."""

    def cfg_echo(self):
        """Un seul flow exposé, sans nœud à problème : l'état de référence « tout va bien »."""
        return self.cfg(FLOWISE_MCP_ALLOW="mcp-echo")

    def test_aucun_probleme_quand_tout_va_bien(self):
        FakeFlowise.executions = [execution(("startAgentflow_0", "Start", "FINISHED", None), state="FINISHED")]
        with mock.patch.dict(os.environ, {"HERMES_HOME": home_with([healthy_job()])}):
            err, text = self.call("flowise_doctor", {}, self.cfg_echo())
        self.assertFalse(err)
        self.assertIn("Aucun problème détecté", text)

    def test_signale_job_en_erreur_et_flow_en_echec(self):
        FakeFlowise.executions = [execution(("customFunctionAgentflow_0", "Analyser", "ERROR", "Error: BOOM"))]
        casse = healthy_job(name="Veille crypto", last_status="error", last_error="Script exited with code 1", failure_streak=2)
        with mock.patch.dict(os.environ, {"HERMES_HOME": home_with([casse])}):
            err, text = self.call("flowise_doctor", {}, self.cfg_echo())
        self.assertFalse(err)
        self.assertIn("Problèmes détectés", text)
        self.assertIn("Veille crypto", text)   # le job
        self.assertIn("Analyser", text)        # le nœud fautif du flow
        self.assertIn("BOOM", text)
        self.assertLess(text.index("ÉCHEC"), text.index("ATTENTION"))  # les échecs d'abord

    def test_signale_un_job_en_pause(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": home_with([healthy_job(enabled=False, state="paused", paused_at="2026-10-26T21:22:00Z")])}):
            _, text = self.call("flowise_doctor", {}, self.cfg_echo())
        self.assertIn("en pause", text)
        self.assertIn("aucune alerte ne partira", text)

    def test_signale_un_flow_structurellement_casse(self):
        FLOWS.append(flow("id-casse", "mcp-casse", [{"name": "startAgentflow"},
                                                    {"name": "directReplyAgentflow"},
                                                    {"name": "httpAgentflow", "label": "HTTP", "inputs": {}}]))
        try:
            FakeFlowise.executions = [execution(("startAgentflow_0", "Start", "FINISHED", None), state="FINISHED")]
            with mock.patch.dict(os.environ, {"HERMES_HOME": home_with([healthy_job()])}):
                _, text = self.call("flowise_doctor", {}, self.cfg(FLOWISE_MCP_ALLOW="mcp-casse"))
            self.assertIn("mcp-casse", text)
            self.assertIn("URL vide", text)
        finally:
            FLOWS.pop()

    def test_all_decrit_aussi_ce_qui_va_bien(self):
        FakeFlowise.executions = [execution(("startAgentflow_0", "Start", "FINISHED", None), state="FINISHED")]
        with mock.patch.dict(os.environ, {"HERMES_HOME": home_with([healthy_job()])}):
            _, text = self.call("flowise_doctor", {"all": True}, self.cfg_echo())
        self.assertIn("mcp-echo", text)
        self.assertIn("job(s) Hermes", text)

    def test_flowise_injoignable_est_signale(self):
        err, text = self.call("flowise_doctor", {}, self.cfg(FLOWISE_URL="http://127.0.0.1:9"))
        self.assertFalse(err)
        self.assertIn("ÉCHEC", text)
        self.assertIn("injoignable", text)

    def test_jobs_illisibles_ne_cassent_pas_le_diagnostic(self):
        FakeFlowise.executions = [execution(("startAgentflow_0", "Start", "FINISHED", None), state="FINISHED")]
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/dossier/qui/nexiste/pas"}):
            err, text = self.call("flowise_doctor", {"all": True}, self.cfg_echo())
        self.assertFalse(err)
        self.assertIn("Aucun problème détecté", text)
        self.assertIn("jobs Hermes illisibles", text)

    def test_ligne_de_commande_silencieuse_quand_tout_va_bien(self):
        """Convention watchdog : sortie vide = aucun message envoyé par la tâche planifiée."""
        FakeFlowise.executions = [execution(("startAgentflow_0", "Start", "FINISHED", None), state="FINISHED")]
        env = {**os.environ, "FLOWISE_URL": self.url, "FLOWISE_API_KEY": "secret",
               "HERMES_HOME": home_with([healthy_job()]), "FLOWISE_MCP_ALLOW": "mcp-echo"}
        silencieux = subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "--doctor"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(silencieux.returncode, 0)
        self.assertEqual(silencieux.stdout.strip(), "")
        detaille = subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "--doctor", "--all"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(detaille.returncode, 0)
        self.assertIn("aucun problème", detaille.stdout)

    def test_ligne_de_commande_signale_les_problemes(self):
        FakeFlowise.executions = [execution(("customFunctionAgentflow_0", "Analyser", "ERROR", "Error: BOOM"))]
        env = {**os.environ, "FLOWISE_URL": self.url, "FLOWISE_API_KEY": "secret",
               "HERMES_HOME": home_with([healthy_job()]), "FLOWISE_MCP_ALLOW": "mcp-echo"}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "--doctor"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Analyser", r.stdout)


    def test_option_inconnue_est_refusee_bruyamment(self):
        """Sans ce garde-fou, une faute de frappe démarrerait le serveur stdio, qui sortirait en 0 en silence."""
        r = subprocess.run([sys.executable, os.path.join(ROOT, "server.py"), "--docteur"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Option inconnue", r.stderr)


class ExecutionFilterTest(Base):
    """Régression : `chatflowId` est ignoré par Flowise, seul `agentflowId` filtre.

    Sans refiltrage côté client, l'échec d'un AUTRE flow était attribué au flow interrogé — constaté
    sur l'installation réelle, où une erreur vieille de 4 minutes apparaissait sur les trois flows.
    """

    @staticmethod
    def autre_flow(label, erreur):
        maintenant = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {"id": "exec-autre", "agentflowId": "un-autre-flow", "state": "ERROR", "createdDate": maintenant,
                "executionData": json.dumps([{"nodeId": "n", "nodeLabel": label, "status": "ERROR", "data": {"error": erreur}}])}

    def test_le_diagnostic_n_attribue_pas_l_echec_d_un_autre_flow(self):
        FakeFlowise.executions = [self.autre_flow("Ailleurs", "pas mon problème")]
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "Boom"})
        err, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertTrue(err)
        self.assertRegex(text, "HTTP 500.*Boom")   # l'erreur réelle reste
        self.assertNotIn("Ailleurs", text)          # mais la trace d'un autre flow n'est pas reprise

    def test_le_doctor_n_attribue_pas_l_echec_d_un_autre_flow(self):
        FakeFlowise.executions = [self.autre_flow("Ailleurs", "pas mon problème")]
        with mock.patch.dict(os.environ, {"HERMES_HOME": home_with([healthy_job()])}):
            _, text = self.call("flowise_doctor", {}, self.cfg(FLOWISE_MCP_ALLOW="mcp-echo"))
        self.assertNotIn("Ailleurs", text)
        self.assertIn("Aucun problème détecté", text)

    def test_une_execution_sans_agentflowid_reste_acceptee(self):
        """Les versions anciennes de Flowise n'exposent pas ce champ : on ne doit rien casser."""
        FakeFlowise.executions = [execution(("customFunctionAgentflow_0", "Analyser", "ERROR", "Error: BOOM"))]
        FakeFlowise.behavior["/api/v1/prediction/id-echo"] = (500, {"message": "Boom"})
        _, text = self.call("flowise_run_flow", {"flow": "mcp-echo", "question": "x"})
        self.assertIn("Analyser", text)


if __name__ == "__main__":
    unittest.main()
