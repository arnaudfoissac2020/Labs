# Canonical ABI definitions for Morpho Blue contracts and associated oracles.
# Import from any script with:
#   import sys, os
#   sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
#   from shared.morpho_abis import MORPHO_ABI, MORPHO_EVENTS_ABI, ...

# ── Morpho Blue core ──────────────────────────────────────────────────────────

MORPHO_ABI = [
    {
        "name": "market",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "id", "type": "bytes32"}],
        "outputs": [
            {"name": "totalSupplyAssets", "type": "uint128"},
            {"name": "totalSupplyShares", "type": "uint128"},
            {"name": "totalBorrowAssets", "type": "uint128"},
            {"name": "totalBorrowShares", "type": "uint128"},
            {"name": "lastUpdate",        "type": "uint128"},
            {"name": "fee",               "type": "uint128"},
        ]
    },
    {
        "name": "position",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "id",   "type": "bytes32"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {"name": "supplyShares", "type": "uint256"},
            {"name": "borrowShares", "type": "uint128"},
            {"name": "collateral",   "type": "uint128"},
        ]
    },
    {
        "name": "idToMarketParams",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "id", "type": "bytes32"}],
        "outputs": [
            {"name": "loanToken",       "type": "address"},
            {"name": "collateralToken", "type": "address"},
            {"name": "oracle",          "type": "address"},
            {"name": "irm",             "type": "address"},
            {"name": "lltv",            "type": "uint256"},
        ]
    },
]

# Explicit single-function alias for scripts that only need market parameters.
MORPHO_PARAMS_ABI = [
    {
        "name": "idToMarketParams",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "id", "type": "bytes32"}],
        "outputs": [
            {"name": "loanToken",       "type": "address"},
            {"name": "collateralToken", "type": "address"},
            {"name": "oracle",          "type": "address"},
            {"name": "irm",             "type": "address"},
            {"name": "lltv",            "type": "uint256"},
        ]
    }
]

# ── Morpho Blue events (all 6 operation types) ────────────────────────────────
# Scripts needing only a subset (Supply/Borrow, etc.) can still use this full
# ABI — web3.py event filters match by signature, not ABI length.

MORPHO_EVENTS_ABI = [
    {
        "name": "Supply",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "id",       "type": "bytes32", "indexed": True},
            {"name": "caller",   "type": "address", "indexed": False},
            {"name": "onBehalf", "type": "address", "indexed": True},
            {"name": "assets",   "type": "uint256", "indexed": False},
            {"name": "shares",   "type": "uint256", "indexed": False},
        ]
    },
    {
        "name": "Withdraw",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "id",       "type": "bytes32", "indexed": True},
            {"name": "caller",   "type": "address", "indexed": False},
            {"name": "onBehalf", "type": "address", "indexed": True},
            {"name": "receiver", "type": "address", "indexed": True},
            {"name": "assets",   "type": "uint256", "indexed": False},
            {"name": "shares",   "type": "uint256", "indexed": False},
        ]
    },
    {
        "name": "Borrow",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "id",       "type": "bytes32", "indexed": True},
            {"name": "caller",   "type": "address", "indexed": False},
            {"name": "onBehalf", "type": "address", "indexed": True},
            {"name": "receiver", "type": "address", "indexed": True},
            {"name": "assets",   "type": "uint256", "indexed": False},
            {"name": "shares",   "type": "uint256", "indexed": False},
        ]
    },
    {
        "name": "Repay",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "id",       "type": "bytes32", "indexed": True},
            {"name": "caller",   "type": "address", "indexed": False},
            {"name": "onBehalf", "type": "address", "indexed": True},
            {"name": "assets",   "type": "uint256", "indexed": False},
            {"name": "shares",   "type": "uint256", "indexed": False},
        ]
    },
    {
        "name": "SupplyCollateral",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "id",       "type": "bytes32", "indexed": True},
            {"name": "caller",   "type": "address", "indexed": False},
            {"name": "onBehalf", "type": "address", "indexed": True},
            {"name": "assets",   "type": "uint256", "indexed": False},
        ]
    },
    {
        "name": "WithdrawCollateral",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "id",       "type": "bytes32", "indexed": True},
            {"name": "caller",   "type": "address", "indexed": False},
            {"name": "onBehalf", "type": "address", "indexed": True},
            {"name": "receiver", "type": "address", "indexed": True},
            {"name": "assets",   "type": "uint256", "indexed": False},
        ]
    },
]

# ── Morpho governance & liquidation events ────────────────────────────────────

MORPHO_GOV_EVENTS_ABI = [
    {
        "name": "EnableIrm",
        "type": "event",
        "inputs": [
            {"name": "irm", "type": "address", "indexed": True}
        ]
    },
    {
        "name": "EnableLltv",
        "type": "event",
        "inputs": [
            {"name": "lltv", "type": "uint256", "indexed": False}
        ]
    },
    {
        "name": "SetFee",
        "type": "event",
        "inputs": [
            {"name": "id",     "type": "bytes32", "indexed": True},
            {"name": "newFee", "type": "uint256", "indexed": False},
        ]
    },
    {
        "name": "Liquidate",
        "type": "event",
        "inputs": [
            {"name": "id",            "type": "bytes32", "indexed": True},
            {"name": "caller",        "type": "address", "indexed": False},
            {"name": "borrower",      "type": "address", "indexed": True},
            {"name": "repaidAssets",  "type": "uint256", "indexed": False},
            {"name": "repaidShares",  "type": "uint256", "indexed": False},
            {"name": "seizedAssets",  "type": "uint256", "indexed": False},
            {"name": "badDebtAssets", "type": "uint256", "indexed": False},
            {"name": "badDebtShares", "type": "uint256", "indexed": False},
        ]
    },
]

# ── Morpho IOracle interface ───────────────────────────────────────────────────

MORPHO_ORACLE_ABI = [
    {
        "name": "price",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [],
        "outputs": [{"name": "", "type": "uint256"}]
    }
]

# ── Chainlink price feeds ─────────────────────────────────────────────────────

CHAINLINK_ABI = [
    {
        "name": "latestRoundData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "roundId",         "type": "uint80"},
            {"name": "answer",          "type": "int256"},
            {"name": "startedAt",       "type": "uint256"},
            {"name": "updatedAt",       "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ]
    },
    {
        "name": "description",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [],
        "outputs": [{"name": "", "type": "string"}]
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [],
        "outputs": [{"name": "", "type": "uint8"}]
    },
]

# ── Multicall3 ────────────────────────────────────────────────────────────────

MULTICALL3_ABI = [
    {
        "name": "aggregate",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target",   "type": "address"},
                    {"name": "callData", "type": "bytes"},
                ]
            }
        ],
        "outputs": [
            {"name": "blockNumber", "type": "uint256"},
            {"name": "returnData",  "type": "bytes[]"},
        ]
    }
]
