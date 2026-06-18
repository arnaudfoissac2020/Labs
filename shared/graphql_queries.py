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
