import ast

import yaml

from app.config import ROOT


def test_compose_services_healthchecks_and_seed_mount():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    assert services["api"]["build"] == "."
    assert services["api"]["depends_on"]["ledger"]["condition"] == "service_healthy"
    for name in ["api", "ledger"]:
        health = services[name]["healthcheck"]
        assert {"test", "interval", "timeout", "retries", "start_period"} <= health.keys()
    assert "pg_isready" in str(services["ledger"]["healthcheck"]["test"])
    assert "http://localhost:8080/health" in str(services["api"]["healthcheck"]["test"])
    assert any("./seeds:" in volume for volume in services["ledger"]["volumes"])


def test_every_application_environment_variable_is_documented():
    documented = {
        line.split("=", 1)[0] for line in (ROOT / ".env.example").read_text().splitlines() if "=" in line
    }
    read = set()
    for path in (ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
            ):
                read.add(ast.literal_eval(node.args[0]))
    assert read <= documented
    assert {
        "CELESTO_PROVIDER",
        "CELESTO_API_KEY",
        "LLM_API_KEY",
        "LLM_MODEL",
        "DATABASE_URL",
        "API_PORT",
    } <= documented
    assert not (ROOT / ".env").exists()
