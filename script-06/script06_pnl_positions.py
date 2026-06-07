"""
SCRIPT 6 — Calcul du P&L quotidien d'une position Morpho Blue

Contexte : Les intérêts sur Morpho Blue s'accumulent à chaque bloc (~12s)
           via un mécanisme de shares. Le Supply APY effectivement perçu
           est la composition des taux instantanés successifs, pas
           l'application d'un taux fixe à une valeur nominale.

           Ce script calcule le P&L quotidien d'un portefeuille de positions
           Morpho Blue en distinguant :
           - Les intérêts perçus / payés (accrual on-chain)
           - Les gas fees (coût opérationnel de règlement)
           - La variation de valorisation du collatéral (P&L de marché latent)

Formule centrale d'accrual (cf. Section IV.4 du mémoire) :
    Assets(t) = supplyShares(t) × totalSupplyAssets(t) / totalSupplyShares(t)
    Intérêts  = Assets(t₁) − Assets(t₀)

Approche :
    Données historiques via eth_call à un bloc spécifique (méthode primaire)
    ou via The Graph subgraph Morpho (méthode alternative).

Dépendances :
    pip install web3 python-dotenv requests

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
  - Morpho Docs, Variable Rate Market, 2025 — https://docs.morpho.org/learn/concepts/market/
  - Morpho Docs, Interest Rate Model, 2025 — https://docs.morpho.org/learn/concepts/irm/
  - BIS Quarterly Review, décembre 2021 — https://www.bis.org/publ/qtrpdf/r_qt2112b.htm
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL   = os.getenv("RPC_ETHEREUM_PUBLIC", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── ADRESSES ─────────────────────────────────────────────────────────────────

MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
CHAINLINK_ETH_USD   = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"
CHAINLINK_USDC_USD  = "0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6"

# ─── RÉFÉRENTIEL TOKENS ────────────────────────────────────────────────────────

TOKEN_INFO = {
    "0x5F7827FDeb7c20b443265Fc2F40845B715385Ff2": {"symbol": "EURCV",   "decimals": 18},
    "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": {"symbol": "cbBTC",   "decimals": 8},
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": {"symbol": "USDC",   "decimals": 6},
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": {"symbol": "USDT",   "decimals": 6},
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": {"symbol": "WETH",   "decimals": 18},
    "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0": {"symbol": "wstETH", "decimals": 18},
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {"symbol": "WBTC",   "decimals": 8},
    "0x6B175474E89094C44Da98b954EedeAC495271d0F": {"symbol": "DAI",    "decimals": 18},
}

# ─── ABIs ─────────────────────────────────────────────────────────────────────

MORPHO_ABI = [
    # Lire l'état d'un marché à un bloc donné (appel historique)
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
    # Lire les positions d'un wallet dans un marché
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
    # Paramètres immuables du marché
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
]


# ═══════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def get_bloc_par_timestamp(timestamp_utc: datetime) -> int:
    """
    Estime le numéro de bloc Ethereum correspondant à un timestamp.

    Ethereum produit environ 1 bloc toutes les 12 secondes.
    On part du bloc actuel et on remonte dans le temps.

    En production, utiliser l'API Etherscan :
    https://api.etherscan.io/api?module=block&action=getblocknobytime

    Args:
        timestamp_utc : datetime en UTC

    Returns:
        Numéro de bloc estimé
    """
    ts_cible  = int(timestamp_utc.timestamp())
    ts_actuel = w3.eth.get_block("latest")["timestamp"]
    bloc_actuel = w3.eth.block_number

    # Calcul par interpolation linéaire (~12 secondes par bloc)
    SECONDES_PAR_BLOC = 12
    delta_secondes = ts_actuel - ts_cible
    delta_blocs    = int(delta_secondes / SECONDES_PAR_BLOC)

    bloc_estime = max(0, bloc_actuel - delta_blocs)

    # Vérification grossière : l'estimation est à ±5% acceptable pour le P&L EOD
    return bloc_estime


def get_eth_price_usd() -> float :
    """Lit le prix ETH/USD depuis Chainlink."""
    try:
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_ETH_USD),
            abi=CHAINLINK_ABI
        )
        data = feed.functions.latestRoundData().call()
        return data[1] / 1e8
    except Exception as ex:
        raise Exception ("Error getting spot price eth/USD", ex)


def get_usdc_price_usd() -> float:
    """Lit le prix USDC/USD depuis Chainlink (quasi 1.00)."""
    try:
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_USDC_USD),
            abi=CHAINLINK_ABI
        )
        data = feed.functions.latestRoundData().call()
        return data[1] / 1e8
    except Exception:
        return 1.0  # Fallback : 1 USDC = 1 USD


def lire_etat_marche(morpho, market_id_bytes: bytes, bloc: int) -> dict:
    """
    Lit l'état d'un marché Morpho Blue à un bloc historique spécifique.

    Utilise eth_call avec block_identifier pour accéder à l'état passé
    de la blockchain — fonctionnalité native de l'EVM.

    Args:
        morpho         : contrat Morpho Blue Web3
        market_id_bytes: ID du marché en bytes32
        bloc           : numéro de bloc historique

    Returns:
        Dictionnaire avec totalSupplyAssets, totalSupplyShares, etc.
    """
    try:
        data = morpho.functions.market(market_id_bytes).call(
            block_identifier=bloc
        )
        return {
            "total_supply_assets": data[0],
            "total_supply_shares": data[1],
            "total_borrow_assets": data[2],
            "total_borrow_shares": data[3],
            "last_update":         data[4],
            "fee":                 data[5],
        }
    except Exception as e:
        raise Exception ("Error getting market state", e)


def lire_position_wallet(
    morpho, market_id_bytes: bytes, wallet: str, bloc: int
) -> dict :
    """
    Lit les positions (supply shares, borrow shares, collateral) d'un wallet
    dans un marché Morpho Blue à un bloc historique spécifique.
    """
    try:
        data = morpho.functions.position(
            market_id_bytes,
            Web3.to_checksum_address(wallet)
        ).call(block_identifier=bloc)
        return {
            "supply_shares": data[0],
            "borrow_shares": data[1],
            "collateral":    data[2],
        }
    except Exception as e:
        raise Exception ("Error getting wallet position", e)


def shares_to_assets(shares: int, total_assets: int, total_shares: int) -> float:
    """
    Convertit des shares Morpho en actifs sous-jacents.

    C'est la formule centrale de l'accrual Morpho Blue :
        Assets = shares × totalAssets / totalShares

    Les intérêts s'accumulent via la croissance du ratio totalAssets/totalShares :
    à mesure que les emprunteurs paient des intérêts, totalBorrowAssets augmente,
    ce qui augmente totalSupplyAssets (via l'IRM), ce qui augmente le ratio
    et donc la valeur des supply shares détenus par les prêteurs.

    Note : Morpho utilise un arrondi "vers le haut" pour les emprunts (toAssetsUp)
    et "vers le bas" pour les dépôts (toAssetsDown) pour favoriser les prêteurs.
    """
    if total_shares == 0:
        return 0.0
    return shares * total_assets / total_shares


def calculer_gas_fees_periode(
    wallet: str,
    market_id_hex: str,
    bloc_debut: int,
    bloc_fin: int,
    eth_price: float ,
) -> dict:
    """
    Agrège les gas fees payées par un wallet pour ses opérations Morpho
    sur une période donnée (bloc_debut → bloc_fin).

    Les gas fees sont un coût opérationnel distinct du taux d'intérêt —
    à comptabiliser séparément en charges (cf. Section IV.5 du mémoire).

    Returns:
        Dictionnaire avec gas_total_eth, gas_total_usd, nb_transactions
    """
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_EVENTS_ABI
    )

    gas_total_wei  = 0
    nb_tx          = 0
    tx_vues        = set()  # éviter de compter deux fois la même tx

    wallet_cs      = Web3.to_checksum_address(wallet)
    market_id_bytes = bytes.fromhex(market_id_hex[2:])

    # Parcourir les événements Supply et Borrow (les plus fréquents)
    for event_name in ["Supply", "Borrow"]:
        try:
            evenements = morpho.events[event_name].get_logs(
                from_block=bloc_debut,
                to_block=bloc_fin,
                argument_filters={"onBehalf": wallet_cs}
            )
            for evt in evenements:
                tx_hash = evt["transactionHash"].hex()
                if tx_hash in tx_vues:
                    continue
                tx_vues.add(tx_hash)

                try:
                    receipt = w3.eth.get_transaction_receipt(tx_hash)
                    gas_wei = receipt["gasUsed"] * receipt.get(
                        "effectiveGasPrice", 0
                    )
                    gas_total_wei += gas_wei
                    nb_tx += 1
                except Exception as e1:
                    print("Exception while parsing events", e1)
                    continue

        except Exception as e2 :
            print("Exception while parsing events", e2)
            continue

    gas_total_eth = gas_total_wei / 1e18
    gas_total_usd = gas_total_eth * eth_price if eth_price else None

    return {
        "gas_total_eth": round(gas_total_eth, 8),
        "gas_total_usd": round(gas_total_usd, 4) if gas_total_usd else None,
        "nb_transactions": nb_tx,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CALCUL DU P&L QUOTIDIEN
# ═══════════════════════════════════════════════════════════════════════════════

def calculer_pnl_position(
    wallet: str,
    market_id_hex: str,
    date_pnl: datetime ,
    inclure_gas: bool = True,
) -> dict:
    """
    Calcule le P&L quotidien d'une position Morpho Blue pour un wallet.

    Décomposition du P&L en trois composantes :
    1. Intérêts perçus (supply) ou payés (borrow) — accrual on-chain
    2. Gas fees — coût opérationnel de règlement (charges distinctes)
    3. Variation de valorisation du collatéral (P&L de marché latent)

    Méthode d'accrual :
    - t₀ = EOD du jour J-1 (bloc correspondant à 23h59 UTC)
    - t₁ = EOD du jour J   (bloc correspondant à 23h59 UTC)
    - Intérêts = [shares × totalAssets/totalShares]_t₁
                 - [shares × totalAssets/totalShares]_t₀

    Args:
        wallet         : adresse Ethereum du wallet institutionnel
        market_id_hex  : ID du marché Morpho Blue (bytes32 en hex)
        date_pnl       : date de calcul (défaut : aujourd'hui UTC)
        inclure_gas    : inclure les gas fees dans le rapport

    Returns:
        Dictionnaire P&L structuré compatible avec les moteurs P&L existants
    """
    if date_pnl is None:
        date_pnl = datetime.now(tz=timezone.utc).replace(
            hour=23, minute=59, second=0, microsecond=0
        )

    date_t0 = date_pnl - timedelta(days=1)

    print(f"\n{'─'*62}")
    print(f"CALCUL P&L — {date_pnl.strftime('%Y-%m-%d')}")
    print(f"  Wallet    : {wallet[:20]}...{wallet[-6:]}")
    print(f"  Marché ID : {market_id_hex[:20]}...")
    print(f"  t₀        : {date_t0.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  t₁        : {date_pnl.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'─'*62}")

    # ── Détermination des blocs t₀ et t₁ ─────────────────────────────────────
    bloc_t1 = w3.eth.block_number
    bloc_t0 = get_bloc_par_timestamp(date_t0)

    print(f"  Bloc t₀   : #{bloc_t0:,}  (≈ {date_t0.strftime('%Y-%m-%d %H:%M')})")
    print(f"  Bloc t₁   : #{bloc_t1:,}  (≈ {date_pnl.strftime('%Y-%m-%d %H:%M')})")

    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI
    )
    market_id_bytes = bytes.fromhex(market_id_hex[2:])

    # ── Lecture des paramètres immuables du marché ────────────────────────────
    try:
        params = morpho.functions.idToMarketParams(market_id_bytes).call()
        loan_token       = params[0]
        collateral_token = params[1]
        lltv             = params[4] / 1e18

        loan_info     = TOKEN_INFO.get(
            Web3.to_checksum_address(loan_token),
            {"symbol": "UNKNOWN", "decimals": 18}
        )
        coll_info     = TOKEN_INFO.get(
            Web3.to_checksum_address(collateral_token),
            {"symbol": "UNKNOWN", "decimals": 18}
        )
        loan_symbol   = loan_info["symbol"]
        loan_decimals = loan_info["decimals"]
        coll_symbol   = coll_info["symbol"]
        coll_decimals = coll_info["decimals"]

        print(f"  Marché    : {loan_symbol}/{coll_symbol} (LLTV {lltv*100:.1f}%)")

    except Exception as e:
        return {"erreur": f"Impossible de lire les paramètres du marché : {e}"}

    # ── Lecture des états t₀ et t₁ ────────────────────────────────────────────
    etat_t0 = lire_etat_marche(morpho, market_id_bytes, bloc_t0)
    etat_t1 = lire_etat_marche(morpho, market_id_bytes, bloc_t1)
    pos_t0  = lire_position_wallet(morpho, market_id_bytes, wallet, bloc_t0)
    pos_t1  = lire_position_wallet(morpho, market_id_bytes, wallet, bloc_t1)

    if not all([etat_t0, etat_t1, pos_t0, pos_t1]):
        return {"erreur": "Impossible de lire l'état on-chain — vérifier le RPC"}

    # ── Calcul de l'accrual des intérêts ──────────────────────────────────────
    #
    # SUPPLY (prêteur) — intérêts perçus
    # Isoler l'effet ratio (intérêts) de l'effet shares (dépôts/retraits).
    # interets = shares_t0 × (ratio_t1 - ratio_t0)
    # flux_capital = (shares_t1 - shares_t0) × ratio_t1
    #
    ratio_supply_t0 = (etat_t0["total_supply_assets"] / etat_t0["total_supply_shares"]
                       if etat_t0["total_supply_shares"] > 0 else 0)
    ratio_supply_t1 = (etat_t1["total_supply_assets"] / etat_t1["total_supply_shares"]
                       if etat_t1["total_supply_shares"] > 0 else 0)
    interets_supply_raw = pos_t0["supply_shares"] * (ratio_supply_t1 - ratio_supply_t0)
    interets_supply = interets_supply_raw / (10**loan_decimals)
    flux_supply_raw = (pos_t1["supply_shares"] - pos_t0["supply_shares"]) * ratio_supply_t1
    flux_supply = flux_supply_raw / (10**loan_decimals)

    # BORROW (emprunteur) — intérêts payés
    # Même décomposition : effet ratio (intérêts) vs effet shares (emprunts/remboursements).
    # interets = shares_t0 × (ratio_t1 - ratio_t0)
    # flux_capital = (shares_t1 - shares_t0) × ratio_t1
    #
    ratio_borrow_t0 = (etat_t0["total_borrow_assets"] / etat_t0["total_borrow_shares"]
                       if etat_t0["total_borrow_shares"] > 0 else 0)
    ratio_borrow_t1 = (etat_t1["total_borrow_assets"] / etat_t1["total_borrow_shares"]
                       if etat_t1["total_borrow_shares"] > 0 else 0)
    interets_borrow_raw = pos_t0["borrow_shares"] * (ratio_borrow_t1 - ratio_borrow_t0)
    interets_borrow = interets_borrow_raw / (10**loan_decimals)  # charge (positif = payé)
    flux_borrow_raw = (pos_t1["borrow_shares"] - pos_t0["borrow_shares"]) * ratio_borrow_t1
    flux_borrow = flux_borrow_raw / (10**loan_decimals)

    # ── Calcul du Supply APY effectif sur la période ───────────────────────────
    # APY journalier estimé depuis la variation du ratio totalAssets/totalShares
    ratio_t0 = (etat_t0["total_supply_assets"] / etat_t0["total_supply_shares"]
                if etat_t0["total_supply_shares"] > 0 else 1.0)
    ratio_t1 = (etat_t1["total_supply_assets"] / etat_t1["total_supply_shares"]
                if etat_t1["total_supply_shares"] > 0 else 1.0)

    rendement_journalier = (ratio_t1 - ratio_t0) / ratio_t0 if ratio_t0 > 0 else 0
    apy_annualise = rendement_journalier * 365 * 100  # en %

    # ── Collatéral déposé ─────────────────────────────────────────────────────
    collateral_t0 = pos_t0["collateral"] / (10**coll_decimals)
    collateral_t1 = pos_t1["collateral"] / (10**coll_decimals)
    variation_collateral = collateral_t1 - collateral_t0

    # ── Taux d'utilisation du marché ──────────────────────────────────────────
    utilisation_t1 = (
        etat_t1["total_borrow_assets"] / etat_t1["total_supply_assets"] * 100
        if etat_t1["total_supply_assets"] > 0 else 0
    )

    # ── Gas fees de la période ────────────────────────────────────────────────
    eth_price = get_eth_price_usd()
    usdc_price = get_usdc_price_usd()

    gas_data = {"gas_total_eth": 0, "gas_total_usd": None, "nb_transactions": 0}
    if inclure_gas:
        gas_data = calculer_gas_fees_periode(
            wallet, market_id_hex, bloc_t0, bloc_t1, eth_price
        )

    # ── Conversion en USD ─────────────────────────────────────────────────────
    # Pour les actifs USDC : prix Chainlink USDC/USD (~1.00)
    interets_supply_usd = interets_supply * usdc_price if loan_symbol == "USDC" else None
    interets_borrow_usd = interets_borrow * usdc_price if loan_symbol == "USDC" else None

    # P&L net (du point de vue du prêteur) :
    # Intérêts perçus - Gas fees (en USD)
    pnl_net_usd = None
    if interets_supply_usd is not None and gas_data.get("gas_total_usd"):
        pnl_net_usd = interets_supply_usd - gas_data["gas_total_usd"]

    # ── Affichage ─────────────────────────────────────────────────────────────
    supply_t0_h = pos_t0["supply_shares"] * ratio_supply_t0 / (10**loan_decimals)
    supply_t1_h = pos_t1["supply_shares"] * ratio_supply_t1 / (10**loan_decimals)
    borrow_t0_h = pos_t0["borrow_shares"] * ratio_borrow_t0 / (10**loan_decimals)
    borrow_t1_h = pos_t1["borrow_shares"] * ratio_borrow_t1 / (10**loan_decimals)

    print(f"\n  POSITION PRÊTEUR (Supply) :")
    print(f"    Actifs t₀       : {supply_t0_h:>15,.6f} {loan_symbol}")
    print(f"    Actifs t₁       : {supply_t1_h:>15,.6f} {loan_symbol}")
    print(f"    Intérêts perçus : {interets_supply:>15,.6f} {loan_symbol}", end="")
    if interets_supply_usd:
        print(f"  (${interets_supply_usd:,.4f} USD)")
    else:
        print()
    if flux_supply != 0:
        print(f"    Flux capital    : {flux_supply:>+15,.6f} {loan_symbol}  (dépôt/retrait)")
    print(f"    Supply APY eff. : {apy_annualise:>14.4f} %")

    if borrow_t1_h > 0:
        print(f"\n  POSITION EMPRUNTEUR (Borrow) :")
        print(f"    Dette t₀        : {borrow_t0_h:>15,.6f} {loan_symbol}")
        print(f"    Dette t₁        : {borrow_t1_h:>15,.6f} {loan_symbol}")
        print(f"    Intérêts payés  : {interets_borrow:>15,.6f} {loan_symbol}")
        if flux_borrow != 0:
            print(f"    Flux capital    : {flux_borrow:>+15,.6f} {loan_symbol}  (emprunt/remboursement)")
        print(f"    Collatéral t₁   : {collateral_t1:>15,.6f} {coll_symbol}")

    print(f"\n  COÛTS OPÉRATIONNELS :")
    print(f"    Gas fees (ETH)  : {gas_data['gas_total_eth']:>15,.8f} ETH")
    if gas_data.get("gas_total_usd"):
        print(f"    Gas fees (USD)  : {gas_data['gas_total_usd']:>15,.4f} USD")
    print(f"    Nb transactions : {gas_data['nb_transactions']:>15,}")

    print(f"\n  MÉTRIQUES MARCHÉ :")
    print(f"    Utilisation t₁  : {utilisation_t1:>14.2f} %",
          end="  ")
    if utilisation_t1 > 92:
        print("🚨 ALERTE")
    elif utilisation_t1 > 85:
        print("⚠️  Surveiller")
    else:
        print("✅")

    if pnl_net_usd is not None:
        print(f"\n  P&L NET (supply - gas) : ${pnl_net_usd:,.4f} USD")

    # ── Construction du rapport P&L structuré ─────────────────────────────────
    rapport = {

        "metadata": {
            "date_pnl":     date_pnl.strftime("%Y-%m-%d"),
            "wallet":       wallet,
            "market_id":    market_id_hex,
            "marche":       f"{loan_symbol}/{coll_symbol} (LLTV {lltv*100:.1f}%)",
            "bloc_t0":      bloc_t0,
            "bloc_t1":      bloc_t1,
            "calcul_utc":   datetime.utcnow().isoformat(),
        },

        # ── Composante 1 : Intérêts ────────────────────────────────────────────
        "interets": {
            "supply_actifs_t0":     round(supply_t0_h, 8),
            "supply_actifs_t1":     round(supply_t1_h, 8),
            "interets_percus":      round(interets_supply, 8),
            "interets_percus_usd":  round(interets_supply_usd, 6) if interets_supply_usd else None,
            "borrow_dette_t0":      round(borrow_t0_h, 8),
            "borrow_dette_t1":      round(borrow_t1_h, 8),
            "interets_payes":       round(interets_borrow, 8),
            "interets_payes_usd":   round(interets_borrow_usd, 6) if interets_borrow_usd else None,
            "devise":               loan_symbol,
            "methode":              "shares × totalAssets / totalShares — accrual on-chain",
        },

        # ── Composante 2 : Collatéral ─────────────────────────────────────────
        "collateral": {
            "collateral_t0":     round(collateral_t0, 8),
            "collateral_t1":     round(collateral_t1, 8),
            "variation":         round(variation_collateral, 8),
            "devise":            coll_symbol,
            "note": ("Variation de collatéral = dépôts - retraits nets sur la période. "
                     "La variation de valorisation (P&L de marché) nécessite "
                     "les prix Chainlink — cf. Script 5."),
        },

        # ── Composante 3 : Gas fees ───────────────────────────────────────────
        "gas_fees": {
            "gas_total_eth":  gas_data["gas_total_eth"],
            "gas_total_usd":  gas_data.get("gas_total_usd"),
            "nb_transactions": gas_data["nb_transactions"],
            "eth_price_usd":  eth_price,
            "note": ("Gas fees = coût opérationnel de règlement. "
                     "À comptabiliser en charges distinctement des intérêts "
                     "selon IFRS 9 / IAS 2 — cf. Section IV.5 du mémoire."),
        },

        # ── P&L synthétique ───────────────────────────────────────────────────
        "pnl_synthetique": {
            "interets_nets_usd": (
                round(interets_supply_usd - (interets_borrow_usd or 0), 6)
                if interets_supply_usd else None
            ),
            "gas_fees_usd":      gas_data.get("gas_total_usd"),
            "pnl_net_usd":       round(pnl_net_usd, 6) if pnl_net_usd else None,
            "note": ("P&L net = intérêts perçus - intérêts payés - gas fees. "
                     "Hors variation de valorisation du collatéral (P&L latent)."),
        },

        # ── Métriques du marché ───────────────────────────────────────────────
        "metriques_marche": {
            "utilisation_t1_pct":       round(utilisation_t1, 4),
            "supply_apy_effectif_pct":  round(apy_annualise, 6),
            "total_supply_assets_t1":   etat_t1["total_supply_assets"] / (10**loan_decimals),
            "total_borrow_assets_t1":   etat_t1["total_borrow_assets"] / (10**loan_decimals),
            "alerte_utilisation":       utilisation_t1 > 92,
        },
    }

    return rapport


def calculer_pnl_portefeuille(
    wallet: str,
    marches: list[str],
    date_pnl: datetime ,
) -> dict:
    """
    Calcule le P&L quotidien consolidé pour un portefeuille de positions
    Morpho Blue.

    Args:
        wallet    : adresse du wallet institutionnel
        marches   : liste des market IDs (bytes32 hex)
        date_pnl  : date de calcul (défaut : aujourd'hui)

    Returns:
        Rapport P&L consolidé avec détail par marché et synthèse globale
    """
    if date_pnl is None:
        date_pnl = datetime.now(tz=timezone.utc)

    rapports = []
    total_interets_usd   = 0.0
    total_gas_usd        = 0.0
    nb_alertes           = 0

    print(f"\n{'═'*62}")
    print(f"P&L PORTEFEUILLE MORPHO BLUE — {date_pnl.strftime('%Y-%m-%d')}")
    print(f"Wallet : {wallet[:20]}...{wallet[-6:]}")
    print(f"Marchés : {len(marches)}")
    print(f"{'═'*62}")

    for market_id in marches:
        rapport = calculer_pnl_position(wallet, market_id, date_pnl)
        if "erreur" in rapport:
            print(f"  ⚠️  Erreur sur marché {market_id[:16]}... : {rapport['erreur']}")
            continue

        rapports.append(rapport)

        # Agrégation
        interets = rapport["pnl_synthetique"].get("interets_nets_usd")
        gas      = rapport["gas_fees"].get("gas_total_usd")
        if interets:
            total_interets_usd += interets
        if gas:
            total_gas_usd += gas
        if rapport["metriques_marche"].get("alerte_utilisation"):
            nb_alertes += 1

    pnl_net_total = total_interets_usd - total_gas_usd

    print(f"\n{'═'*62}")
    print(f"SYNTHÈSE P&L PORTEFEUILLE")
    print(f"{'═'*62}")
    print(f"  Intérêts nets      : ${total_interets_usd:>14,.6f} USD")
    print(f"  Gas fees totaux    : ${total_gas_usd:>14,.6f} USD")
    print(f"  P&L net total      : ${pnl_net_total:>14,.6f} USD")
    if nb_alertes > 0:
        print(f"\n  ⚠️  {nb_alertes} marché(s) en zone d'alerte (utilisation > 92%)")

    return {
        "date_pnl":           date_pnl.strftime("%Y-%m-%d"),
        "wallet":             wallet,
        "nb_marches":         len(rapports),
        "total_interets_usd": round(total_interets_usd, 6),
        "total_gas_usd":      round(total_gas_usd, 6),
        "pnl_net_total_usd":  round(pnl_net_total, 6),
        "nb_alertes":         nb_alertes,
        "detail_marches":     rapports,
        "calcul_utc":         datetime.utcnow().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("SCRIPT 6 — P&L QUOTIDIEN MORPHO BLUE")
    print("Accrual des intérêts + gas fees + métriques de marché")
    print("Section IV.4 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*62)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")

    # ── Portefeuille institutionnel de démonstration ───────────────────────────
    # Remplacer par les adresses réelles en production
    #WALLET_INSTITUTION = os.getenv(
    #   "WALLET_INSTITUTION",
    #    "0x5130985cE6A0e54f369712Cd6f2fDEC084026E54"
    #)

    WALLET_INSTITUTION = os.getenv(
        "WALLET_INSTITUTION",
    )


    MARCHES_PORTEFEUILLE = [
        # EURCV/cbBTC (LLTV 86%) — marché principal
        "0xb5f8d5554d85b782d7080314bba3544983755a75eb5c432f5eae1c47c6af4da4",
    ]

    # Calcul du P&L pour aujourd'hui
    date_calcul = datetime.now(tz=timezone.utc)

    rapport_portefeuille = calculer_pnl_portefeuille(
        wallet=WALLET_INSTITUTION,
        marches=MARCHES_PORTEFEUILLE,
        date_pnl=date_calcul,
    )

    # ── Export JSON ───────────────────────────────────────────────────────────
    output_file = "morpho_daily_pnl.json"
    with open(output_file, "w") as f:
        json.dump(rapport_portefeuille, f, indent=2, default=str)

    print(f"\n{'━'*62}")
    print(f"💾 Rapport P&L exporté : {output_file}")
    print(f"\nSTRUCTURE DU RAPPORT :")
    for champ, desc in [
        ("date_pnl",           "Date de calcul (EOD)"),
        ("total_interets_usd", "Intérêts nets perçus/payés en USD"),
        ("total_gas_usd",      "Gas fees agrégés en USD (charges opérationnelles)"),
        ("pnl_net_total_usd",  "P&L net = intérêts - gas (hors P&L latent collatéral)"),
        ("detail_marches[]",   "Détail par marché : accrual, shares, métriques, alertes"),
    ]:
        print(f"  {champ:<25} : {desc}")
    print("━"*62)