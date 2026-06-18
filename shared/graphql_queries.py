
import os
from dotenv import load_dotenv

# Canonical GraphQL query definitions for The Graph subgraphs.
# Import from any script with:
#   import sys, os
#   sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
#   from shared.graphql_queries import LENDING_MARKETS_QUERY

# ── Lending markets (Messari schema — compatible Aave V3 and Morpho) ──────────

LENDING_MARKETS_QUERY = """
{
  markets(
    first: 50
    orderBy: totalValueLockedUSD
    orderDirection: desc
  ) {
    id
    name
    inputToken {
      symbol
    }
    totalValueLockedUSD
    totalDepositBalanceUSD
    totalBorrowBalanceUSD
    rates {
      rate
      side
      type
    }
    liquidationThreshold
    canBorrowFrom
    isActive
  }
}
"""

# Subgraphs The Graph pour l'analyse approfondie (Niveau 2)
GRAPH_API_KEY = os.getenv("GRAPH_API_KEY")

SUBGRAPHS = {
    "Aave V3": (
        f"https://gateway.thegraph.com/api/{GRAPH_API_KEY}/subgraphs/id/"
        "JCNWRypm7FYwV8fx5HhzZPSFaMxgkPuw4TnR3Gpi81zk"
    ),
    "Morpho": (
        f"https://gateway.thegraph.com/api/{GRAPH_API_KEY}/subgraphs/id/"
        "8Lz789DP5VKLXumTMTgygjU2xtuzx8AhbaacgN5PYCAs"
    ),
}
