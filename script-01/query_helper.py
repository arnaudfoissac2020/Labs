
import os
import re
import json
from pathlib import Path

import requests

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

# DeFi Llama API — publique, sans authentification
DEFILLAMA_BASE_URL = "https://api.llama.fi"

# Set PRODUCE_MOCK=1 to save raw API responses as JSON fixtures under tests/fixtures/
PRODUCE_MOCK = os.getenv("PRODUCE_MOCK", "").lower() in ("1", "true")
_FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"


def _save_fixture(name: str, data: dict) -> None:
    _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIXTURES_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"   [MOCK] Saved: {path}")


def run_graphql_query(url: str, query: str, variables: dict = None) -> dict:
    """
    Exécute une requête GraphQL sur un subgraph The Graph.

    Args:
        url       : URL du subgraph
        query     : Requête GraphQL
        variables : Variables optionnelles

    Returns:
        Dictionnaire JSON de la réponse (champ "data")
    """
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    response = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()

    if "errors" in result:
        raise ValueError(f"Erreur GraphQL : {result['errors']}")

    data = result["data"]
    if PRODUCE_MOCK:
        match = re.search(r'/id/([A-Za-z0-9]+)', url)
        key = match.group(1)[:12] if match else re.sub(r'[^a-zA-Z0-9]', '_', url)[-12:]
        _save_fixture(f"graphql_{key}", data)
    return data

def fetch_defillama(endpoint: str, params: dict = None) -> dict :
    """
    Appel générique à l'API DeFi Llama.

    DeFi Llama est un agrégateur de données DeFi qui normalise les métriques
    de TVL, volumes et revenus sur l'ensemble des protocoles et blockchains.
    Son API publique est la référence pour un screening institutionnel rapide
    avant une due diligence approfondie.

    Args:
        endpoint : Chemin de l'endpoint (ex: "/protocol/aave-v3")
        params   : Paramètres de requête optionnels

    Returns:
        Données JSON de la réponse
    """
    url = f"{DEFILLAMA_BASE_URL}{endpoint}"
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if PRODUCE_MOCK:
        key = re.sub(r'[^a-zA-Z0-9-]', '_', endpoint).strip('_')
        _save_fixture(f"defillama_{key}", data)
    return data
