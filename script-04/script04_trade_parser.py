"""
SCRIPT 4 — Parsing des événements on-chain Morpho Blue
          et normalisation vers un schéma de trade compatible OMS/TMS

Contexte : Une opération Morpho Blue (supply, borrow, withdraw, repay)
           génère un événement on-chain dont les attributs bruts ne
           correspondent à aucun champ standard d'un système de booking
           comme Murex, Calypso ou Sophis. Ce script extrait ces événements
           depuis la blockchain et les transforme en un objet structuré
           directement injectable dans un OMS/TMS via API.

           Il couvre les 6 types d'opérations Morpho Blue :
           - Supply          : dépôt d'actifs de prêt par un prêteur
           - Withdraw        : retrait d'actifs de prêt par un prêteur
           - Borrow          : emprunt d'actifs contre collatéral
           - Repay           : remboursement de la dette
           - SupplyCollateral: dépôt de collatéral par un emprunteur
           - WithdrawCollateral: retrait de collatéral

Mapping des attributs (cf. Section IV.2 du mémoire) :
  Tx hash           → UTI (Unique Trade Identifier) — format interne
  Adresse Morpho    → LEI contrepartie (convention interne)
  Adresse token ERC-20 → ISIN (référentiel interne de mapping)
  Tx hash + bloc    → Référence de règlement (équivalent MT54x)
  Taux variable     → Taux contractuel (à recalculer à chaque accrual)
  Gas fees (ETH)    → Frais de transaction (coût opérationnel distinct)

Dépendances :
    pip install web3 python-dotenv requests

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
  - Morpho Docs, Variable Rate Market, 2025
    https://docs.morpho.org/learn/concepts/market/
  - ESMA, EMIR Reporting Guidelines, 2024
  - FSB, The Financial Stability Risks of DeFi, 2023
"""

import os
import json
import hashlib
from datetime import datetime, timezone
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_ETHEREUM_PUBLIC", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── ADRESSES ─────────────────────────────────────────────────────────────────

MORPHO_BLUE_ADDRESS  = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
CHAINLINK_ETH_USD    = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"

# Référentiel interne de mapping adresse token → symbole + décimales
# À étendre avec les tokens utilisés par l'institution
TOKEN_REGISTRY = {
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": {"symbol": "USDC",   "decimals": 6},
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": {"symbol": "USDT",   "decimals": 6},
    "0x6B175474E89094C44Da98b954EedeAC495271d0F": {"symbol": "DAI",    "decimals": 18},
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": {"symbol": "WETH",   "decimals": 18},
    "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0": {"symbol": "wstETH", "decimals": 18},
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {"symbol": "WBTC",   "decimals": 8},
    "0xae78736Cd615f374D3085123A210448E74Fc6393": {"symbol": "rETH",   "decimals": 18},
    "0xBe9895146f7AF43049ca1c1AE358B0541Ea49704": {"symbol": "cbETH",  "decimals": 18},
}

# Convention interne : adresse Morpho Blue → LEI fictif
# (En production, à remplacer par la convention définie par le juridique)
MORPHO_INTERNAL_LEI = "MORPHO_BLUE_ETH_MAINNET"
MORPHO_INTERNAL_LEI_NOTE = ("Pas de LEI réel — smart contract sans entité juridique. "
                             "Convention interne requise — cf. Section IV.2 du mémoire.")

# ─── ABIs ─────────────────────────────────────────────────────────────────────

MORPHO_EVENTS_ABI = [
    # Supply — dépôt d'actifs de prêt
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
    # Withdraw — retrait d'actifs de prêt
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
    # Borrow — emprunt d'actifs contre collatéral
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
    # Repay — remboursement de la dette
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
    # SupplyCollateral — dépôt de collatéral
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
    # WithdrawCollateral — retrait de collatéral
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
    }
]


# ═══════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def get_eth_price_usd() -> float:
    """
    Lit le prix ETH/USD depuis le feed Chainlink.
    Utilisé pour convertir les gas fees (en ETH) en USD.
    """
    try:
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_ETH_USD),
            abi=CHAINLINK_ABI
        )
        data = feed.functions.latestRoundData().call()
        # Le feed Chainlink ETH/USD retourne 8 décimales
        return data[1] / 1e8
    except Exception:
        raise Exception ("Impossible to get ETH/USD price")


def generer_uti(tx_hash: str, event_type: str) -> str:
    """
    Génère un identifiant de trade interne au format UTI-like
    depuis le hash de transaction Ethereum.

    Note : cet UTI n'est pas conforme au standard EMIR (qui requiert
    un préfixe LEI + suffixe unique). En production, l'institution doit
    définir sa propre convention de conversion tx_hash → UTI conforme.
    Ce format sert de référence interne jusqu'à validation juridique.

    Format : MORPHO-{TYPE}-{8 premiers chars du tx_hash}
    """
    short_hash = tx_hash[2:10].upper()  # Retire '0x', prend 8 chars
    return f"MORPHO-{event_type[:3]}-{short_hash}"


def lookup_token(address: str) -> dict:
    """
    Résout une adresse de token ERC-20 en ses attributs (symbole, décimales).
    Utilise le référentiel TOKEN_REGISTRY interne.
    """
    addr_normalized = Web3.to_checksum_address(address)
    return TOKEN_REGISTRY.get(
        addr_normalized,
        {"symbol": f"UNKNOWN_{address[:8]}", "decimals": 18}
    )


def get_market_params_cached(market_id_hex: str, cache: dict) -> dict :
    """
    Récupère les paramètres immuables d'un marché Morpho Blue.
    Met en cache pour éviter des appels répétés on-chain.
    """
    if market_id_hex in cache:
        return cache[market_id_hex]

    try:
        morpho = w3.eth.contract(
            address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
            abi=MORPHO_PARAMS_ABI
        )
        market_id_bytes = bytes.fromhex(market_id_hex[2:])
        params = morpho.functions.idToMarketParams(market_id_bytes).call()
        loan_token, collateral_token, oracle, irm, lltv = params

        result = {
            "market_id":        market_id_hex,
            "loan_token":       loan_token,
            "collateral_token": collateral_token,
            "oracle":           oracle,
            "irm":              irm,
            "lltv":             lltv,
            "lltv_pct":         round(lltv / 1e18 * 100, 2),
            "loan_symbol":      lookup_token(loan_token)["symbol"],
            "collateral_symbol":lookup_token(collateral_token)["symbol"],
        }
        cache[market_id_hex] = result
        return result

    except Exception as e:
        raise Exception("Impossible to get Market Params")


# ═══════════════════════════════════════════════════════════════════════════════
# NORMALISATION DES ÉVÉNEMENTS → SCHÉMA DE TRADE OMS/TMS
# ═══════════════════════════════════════════════════════════════════════════════

def normaliser_evenement(
    event: dict,
    event_type: str,
    tx_receipt: dict,
    block: dict,
    market_params: dict ,
    eth_price_usd: float ,
) -> dict:
    """
    Normalise un événement Morpho Blue brut vers un schéma de trade
    standardisé compatible OMS/TMS (Murex, Calypso, Sophis).

    Le schéma couvre les attributs obligatoires d'une opération financière :
    identification, contrepartie, instrument, montant, prix, règlement,
    frais et métadonnées réglementaires.

    Args:
        event        : événement Morpho Blue décodé
        event_type   : type d'opération (SUPPLY, BORROW, etc.)
        tx_receipt   : reçu de transaction Ethereum
        block        : données du bloc
        market_params: paramètres immuables du marché Morpho
        eth_price_usd: prix ETH en USD depuis Chainlink

    Returns:
        Dictionnaire normalisé prêt pour injection dans l'OMS/TMS
    """
    tx_hash  = tx_receipt["transactionHash"].hex()
    args     = event["args"]

    # ── Identification de l'opération ─────────────────────────────────────────
    uti = generer_uti(tx_hash, event_type)

    # ── Attributs du marché ───────────────────────────────────────────────────
    market_id_hex = "0x" + args["id"].hex()

    if market_params:
        loan_symbol       = market_params["loan_symbol"]
        collateral_symbol = market_params["collateral_symbol"]
        loan_token        = market_params["loan_token"]
        collateral_token  = market_params["collateral_token"]
        lltv_pct          = market_params["lltv_pct"]
        oracle            = market_params["oracle"]
    else:
        loan_symbol = collateral_symbol = "UNKNOWN"
        loan_token  = collateral_token  = ""
        lltv_pct    = 0
        oracle      = ""

    # ── Détermination du token concerné et du montant ─────────────────────────
    # Supply/Withdraw/Borrow/Repay → Loan Asset
    # SupplyCollateral/WithdrawCollateral → Collateral Asset
    if event_type in ("SUPPLY_COLLATERAL", "WITHDRAW_COLLATERAL"):
        token_address = collateral_token
        token_info    = lookup_token(collateral_token)
        token_symbol  = collateral_symbol
        sens          = "BUY" if event_type == "SUPPLY_COLLATERAL" else "SELL"
    else:
        token_address = loan_token
        token_info    = lookup_token(loan_token)
        token_symbol  = loan_symbol
        # Convention : SUPPLY = prêt (on donne), BORROW = emprunt (on reçoit)
        sens = "SELL" if event_type in ("SUPPLY", "REPAY") else "BUY"

    # Conversion raw → unités lisibles
    decimals      = token_info["decimals"]
    raw_assets    = args.get("assets", 0)
    raw_shares    = args.get("shares", 0)
    montant_net   = raw_assets / (10**decimals)
    shares_humain = raw_shares / 1e18  # Les shares Morpho ont 18 décimales

    # ── Frais de transaction (gas fees) ───────────────────────────────────────
    gas_used      = tx_receipt.get("gasUsed", 0)
    gas_price_wei = tx_receipt.get("effectiveGasPrice", 0)
    gas_fees_eth  = (gas_used * gas_price_wei) / 1e18
    gas_fees_usd  = (gas_fees_eth * eth_price_usd) if eth_price_usd else None

    # ── Horodatage ────────────────────────────────────────────────────────────
    block_ts    = block.get("timestamp", 0)
    dt_utc      = datetime.fromtimestamp(block_ts, tz=timezone.utc)
    trade_date  = dt_utc.strftime("%Y-%m-%d")
    trade_time  = dt_utc.strftime("%H:%M:%S UTC")

    # ── Schéma normalisé OMS/TMS ──────────────────────────────────────────────
    trade_normalise = {

        # ── IDENTIFICATION ────────────────────────────────────────────────────
        "uti": uti,
        "uti_note": ("Format interne MORPHO-{TYPE}-{HASH}. "
                     "Conversion vers UTI EMIR requise en production."),
        "tx_hash": tx_hash,
        "trade_type": event_type,
        "trade_date": trade_date,
        "trade_time": trade_time,
        "block_number": tx_receipt["blockNumber"],
        "bloc_timestamp_utc": dt_utc.isoformat(),

        # ── CONTREPARTIE ─────────────────────────────────────────────────────
        # Morpho Blue est un smart contract sans entité juridique identifiable
        "counterparty_address": MORPHO_BLUE_ADDRESS,
        "counterparty_lei":     MORPHO_INTERNAL_LEI,
        "counterparty_lei_note": MORPHO_INTERNAL_LEI_NOTE,
        "caller":   args.get("caller", ""),
        "on_behalf": args.get("onBehalf", ""),
        "receiver": args.get("receiver", args.get("onBehalf", "")),

        # ── INSTRUMENT / MARCHÉ ───────────────────────────────────────────────
        # Pas d'ISIN natif — référentiel interne de mapping requis
        "market_id":           market_id_hex,
        "isin_internal":       f"MORPHO_{loan_symbol}_{collateral_symbol}",
        "isin_note":           ("Pas d'ISIN officiel. Convention interne "
                                "MORPHO_{LoanAsset}_{CollateralAsset}."),
        "loan_asset":          loan_symbol,
        "loan_asset_address":  loan_token,
        "collateral_asset":    collateral_symbol,
        "collateral_address":  collateral_token,
        "lltv_pct":            lltv_pct,
        "oracle_address":      oracle,
        "irm":                 "AdaptiveCurveIRM",

        # ── MONTANT ET SENS ───────────────────────────────────────────────────
        "sens":              sens,
        "token_symbol":      token_symbol,
        "token_address":     token_address,
        "montant_net":       round(montant_net, 6),
        "montant_raw":       raw_assets,
        "shares":            round(shares_humain, 6),
        "shares_raw":        raw_shares,
        "devise_reference":  "USD",

        # ── TAUX ─────────────────────────────────────────────────────────────
        # Le taux Morpho est variable, calculé à chaque bloc via l'IRM
        # Il doit être recalculé quotidiennement pour l'accrual (cf. Script 6)
        "taux_type":  "VARIABLE",
        "taux_note":  ("Taux Morpho Adaptive Curve IRM — variable à chaque bloc. "
                       "Recalcul quotidien requis pour l'accrual (cf. Script 6)."),

        # ── RÈGLEMENT ────────────────────────────────────────────────────────
        # Le règlement Morpho est atomique et instantané
        # Le tx hash + numéro de bloc constituent la preuve de règlement
        "settlement_ref":     tx_hash,
        "settlement_ref_note": ("Équivalent fonctionnel d'une référence SWIFT MT54x. "
                                "Règlement atomique on-chain — pas de T+2."),
        "settlement_type":    "ATOMIC_ONCHAIN",
        "settlement_date":    trade_date,
        "settlement_time":    trade_time,

        # ── FRAIS ─────────────────────────────────────────────────────────────
        # Les gas fees sont un coût opérationnel distinct du taux d'intérêt
        # À comptabiliser séparément en charges (cf. Section IV.5)
        "gas_fees_eth":  round(gas_fees_eth, 8),
        "gas_fees_usd":  round(gas_fees_usd, 4) if gas_fees_usd else None,
        "gas_used":      gas_used,
        "gas_price_gwei": round(gas_price_wei / 1e9, 4),
        "gas_note":      ("Gas fees = coût opérationnel de règlement. "
                          "À comptabiliser en charges, pas en spread."),

        # ── MÉTADONNÉES RÉGLEMENTAIRES ────────────────────────────────────────
        "protocole":        "Morpho Blue",
        "reseau":           "Ethereum Mainnet",
        "protocole_adresse": MORPHO_BLUE_ADDRESS,
        "emir_scope":       ("À qualifier — Supply/Borrow hors scope EMIR probable. "
                             "Cf. Section IV.5 du mémoire."),
        "mica_scope":       ("Protocole décentralisé — exemption MiCA probable. "
                             "Vérifier la qualification des actifs (USDC = EMT)."),

        # ── RÉCONCILIATION ────────────────────────────────────────────────────
        "reconciliation_key": f"{tx_hash}_{event_type}",
        "source":             "ONCHAIN_EVENT",
        "export_timestamp":   datetime.utcnow().isoformat(),
    }

    return trade_normalise


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION ET PARSING DES ÉVÉNEMENTS ON-CHAIN
# ═══════════════════════════════════════════════════════════════════════════════

def extraire_trades_morpho(
    adresse_wallet: str,
    nb_blocs: int = 5000,
    types_evenements: list  =[],
) -> list[dict]:
    """
    Extrait et normalise les événements Morpho Blue pour un wallet donné
    sur les derniers N blocs.

    Args:
        adresse_wallet   : adresse Ethereum de l'institution
        nb_blocs         : nombre de blocs à remonter (défaut : 5 000 ≈ 17h)
        types_evenements : liste des types à extraire (None = tous)

    Returns:
        Liste des trades normalisés, triés par bloc décroissant
    """
    if types_evenements is None:
        types_evenements = [
            "Supply", "Withdraw", "Borrow", "Repay",
            "SupplyCollateral", "WithdrawCollateral"
        ]

    # Mapping nom événement → type interne OMS
    EVENT_TO_TYPE = {
        "Supply":             "SUPPLY",
        "Withdraw":           "WITHDRAW",
        "Borrow":             "BORROW",
        "Repay":              "REPAY",
        "SupplyCollateral":   "SUPPLY_COLLATERAL",
        "WithdrawCollateral": "WITHDRAW_COLLATERAL",
    }

    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_EVENTS_ABI
    )

    bloc_actuel = w3.eth.block_number
    bloc_debut  = max(0, bloc_actuel - nb_blocs)
    wallet_cs   = Web3.to_checksum_address(adresse_wallet)

    print(f"\n{'─'*62}")
    print(f"EXTRACTION DES ÉVÉNEMENTS MORPHO BLUE")
    print(f"Wallet         : {wallet_cs}")
    print(f"Période        : blocs #{bloc_debut:,} → #{bloc_actuel:,} ({nb_blocs} blocs)")
    print(f"Types filtrés  : {', '.join(types_evenements)}")
    print(f"{'─'*62}")

    trades       = []
    market_cache = {}
    eth_price    = get_eth_price_usd()

    if eth_price:
        print(f"Prix ETH/USD   : ${eth_price:,.2f} (Chainlink)")
    else:
        print("⚠️  Prix ETH indisponible — gas fees USD non calculés")

    for event_name in types_evenements:
        event_type = EVENT_TO_TYPE[event_name]

        try:
            # eth_getLogs est stateless — supporté par tous les nœuds publics
            # onBehalf est indexé dans tous les événements Morpho Blue
            evenements = morpho.events[event_name].get_logs(
                from_block=bloc_debut,
                to_block=bloc_actuel,
                argument_filters={"onBehalf": wallet_cs}
            )

        except Exception as ex:
            print(f"⚠️  Impossible de récupérer les événements {event_name} : {ex}")
            continue

        if not evenements:
            continue

        print(f"\n  {event_type:<22} : {len(evenements)} événement(s) trouvé(s)")

        for event in evenements:
            try:
                tx_hash    = event["transactionHash"].hex()
                tx_receipt = w3.eth.get_transaction_receipt(tx_hash)
                block      = w3.eth.get_block(event["blockNumber"])

                # Paramètres immuables du marché (avec cache)
                market_id_hex = "0x" + event["args"]["id"].hex()
                market_params = get_market_params_cached(market_id_hex, market_cache)

                # Normalisation vers le schéma OMS/TMS
                trade = normaliser_evenement(
                    event, event_type, tx_receipt, block,
                    market_params, eth_price
                )
                trades.append(trade)
                print(f"    ✅ {trade['uti']} — {trade['montant_net']:,.4f} "
                      f"{trade['token_symbol']} — bloc #{trade['block_number']:,}")

            except Exception as e:
                print(f"    ⚠️  Erreur parsing événement {tx_hash[:16]}... : {e}")
                continue

    # Tri par bloc décroissant (le plus récent en premier)
    trades.sort(key=lambda x: x["block_number"], reverse=True)
    return trades


def parser_transaction_unique(tx_hash: str) -> list[dict]:
    """
    Parse et normalise tous les événements Morpho Blue contenus
    dans une transaction spécifique.

    Utile pour confirmer un trade individuel après exécution
    (cf. Script 9 — confirmation structurée depuis tx hash).

    Args:
        tx_hash : hash de la transaction Ethereum (ex. "0xabc123...")

    Returns:
        Liste des trades normalisés extraits de la transaction
    """
    print(f"\n{'─'*62}")
    print(f"PARSING TRANSACTION : {tx_hash}")
    print(f"{'─'*62}")

    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_EVENTS_ABI
    )

    EVENT_TO_TYPE = {
        "Supply":             "SUPPLY",
        "Withdraw":           "WITHDRAW",
        "Borrow":             "BORROW",
        "Repay":              "REPAY",
        "SupplyCollateral":   "SUPPLY_COLLATERAL",
        "WithdrawCollateral": "WITHDRAW_COLLATERAL",
    }

    try:
        tx_receipt = w3.eth.get_transaction_receipt(tx_hash)
        block      = w3.eth.get_block(tx_receipt["blockNumber"])
        eth_price  = get_eth_price_usd()
        market_cache = {}
        trades = []

        # Décoder tous les logs de la transaction
        for event_name, event_type in EVENT_TO_TYPE.items():
            try:
                decoded = morpho.events[event_name]().process_receipt(tx_receipt)
                for event in decoded:
                    market_id_hex = "0x" + event["args"]["id"].hex()
                    market_params = get_market_params_cached(
                        market_id_hex, market_cache
                    )
                    trade = normaliser_evenement(
                        event, event_type, tx_receipt, block,
                        market_params, eth_price
                    )
                    trades.append(trade)
                    print(f"  ✅ Événement : {event_type}")
                    print(f"     UTI       : {trade['uti']}")
                    print(f"     Montant   : {trade['montant_net']:,.6f} "
                          f"{trade['token_symbol']}")
                    print(f"     Marché    : {trade['loan_asset']}/"
                          f"{trade['collateral_asset']} "
                          f"(LLTV {trade['lltv_pct']}%)")
                    print(f"     Bloc      : #{trade['block_number']:,}")
                    print(f"     Gas fees  : {trade['gas_fees_eth']:.6f} ETH"
                          + (f" (${trade['gas_fees_usd']:.2f})"
                             if trade['gas_fees_usd'] else ""))

            except Exception:
                continue

        if not trades:
            print("  ℹ️  Aucun événement Morpho Blue trouvé dans cette transaction")

        return trades

    except Exception as e:
        print(f"  ❌ Erreur : {e}")
        return []


def afficher_resume(trades: list[dict]) -> None:
    """Affiche un résumé des trades normalisés."""
    if not trades:
        print("\n  Aucun trade trouvé.")
        return

    print(f"\n{'═'*62}")
    print(f"RÉSUMÉ — {len(trades)} TRADES NORMALISÉS")
    print(f"{'═'*62}")
    print(f"{'UTI':<25} {'Type':<22} {'Montant':>14} {'Marché':<18} {'Bloc':>10}")
    print(f"{'─'*62}")

    for t in trades:
        marche = f"{t['loan_asset']}/{t['collateral_asset']}"
        print(f"{t['uti']:<25} {t['trade_type']:<22} "
              f"{t['montant_net']:>14,.4f} "
              f"{marche:<18} "
              f"#{t['block_number']:>9,}")


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("SCRIPT 4 — PARSING ÉVÉNEMENTS MORPHO BLUE → SCHÉMA OMS/TMS")
    print("Normalisation des trades DeFi pour systèmes de booking")
    print("Section IV.2 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*62)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")

   
    # ── MODE 1 : Extraction pour un wallet institutionnel ─────────────────────
    # Remplacer par l'adresse MPC wallet de l'institution en production
    # Ici : wallet de démonstration connu pour avoir des positions Morpho
    TRADING_WALLET = os.getenv("WALLET_WITH_MORPHO_POSITIONS")

    print(f"\n{'═'*62}")
    print("MODE 1 : Extraction des trades pour un wallet institutionnel")
    print(f"{'═'*62}")

    trades = extraire_trades_morpho(
        adresse_wallet=TRADING_WALLET,
        nb_blocs =10_000,    # ~33 heures de blocs
        types_evenements=["Supply", "Withdraw", "Borrow", "Repay"],
    )

    afficher_resume(trades)

    # ── MODE 2 : Parsing d'une transaction spécifique ─────────────────────────
    # Exemple de transaction Morpho Blue réelle (Supply USDC sur wstETH market)
    # Remplacer par un tx hash réel en production
    TX_DEMO = os.getenv(
        "TX_HASH_DEMO",
        "0xa4062ecc827e46966b85886c258ad7fc8f22ca2bd52832797af2249090326bc4"
    )

    if TX_DEMO != "0x" + "0"*64:
        print(f"\n{'═'*62}")
        print("MODE 2 : Parsing d'une transaction spécifique")
        print(f"{'═'*62}")
        trades_tx = parser_transaction_unique(TX_DEMO)
        if trades_tx:
            trades.extend(trades_tx)

    # ── Export JSON ───────────────────────────────────────────────────────────
    output_file = "morpho_trades_normalized.json"
    export = {
        "metadata": {
            "script":         "Script 4 — Parsing Morpho Blue → OMS/TMS",
            "protocole":      "Morpho Blue",
            "adresse":        MORPHO_BLUE_ADDRESS,
            "reseau":         "Ethereum Mainnet",
            "wallet_filtre":  TRADING_WALLET,
            "export_date":    datetime.utcnow().isoformat(),
            "nb_trades":      len(trades),
            "note_booking": (
                "Ce fichier JSON constitue la source d'alimentation du système "
                "de booking. Chaque entrée correspond à une opération Morpho Blue "
                "normalisée avec ses attributs OMS/TMS. À injecter via l'API "
                "d'import du système interne (Murex, Calypso, Sophis)."
            )
        },
        "trades": trades,
    }

    with open(output_file, "w") as f:
        json.dump(export, f, indent=2, default=str)

    print(f"\n{'━'*62}")
    print(f"💾 {len(trades)} trades exportés : {output_file}")
    print(f"\nSTRUCTURE DU SCHÉMA NORMALISÉ (champs principaux) :")
    champs = [
        ("uti",              "Identifiant unique interne (UTI-like depuis tx hash)"),
        ("tx_hash",          "Preuve de règlement on-chain (équivalent MT54x)"),
        ("trade_type",       "SUPPLY / BORROW / WITHDRAW / REPAY / ..."),
        ("loan_asset",       "Actif de prêt (symbole depuis référentiel interne)"),
        ("collateral_asset", "Actif collatéral"),
        ("montant_net",      "Montant en unités lisibles (décimales appliquées)"),
        ("gas_fees_usd",     "Frais de règlement en USD (coût opérationnel)"),
        ("settlement_ref",   "Référence de règlement = tx_hash"),
        ("settlement_type",  "ATOMIC_ONCHAIN — pas de T+2"),
        ("emir_scope",       "Qualification EMIR à valider avec l'équipe juridique"),
    ]
    for champ, desc in champs:
        print(f"  {champ:<22} : {desc}")
    print("━"*62)