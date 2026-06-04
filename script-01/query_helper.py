
import requests

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

# DeFi Llama API — publique, sans authentification
DEFILLAMA_BASE_URL = "https://api.llama.fi"


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

    return result["data"]

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
    return response.json()
