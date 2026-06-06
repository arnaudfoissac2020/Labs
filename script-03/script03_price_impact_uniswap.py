"""
SCRIPT 3 — Price impact & optimisation du routing Uniswap V3
          avant dépôt sur un marché Morpho Blue

VERSION CORRIGÉE : le prix spot est lu directement depuis slot0() du contrat
de pool (sqrtPriceX96), ce qui donne le prix marginal exact au tick courant,
sans approximation par un mini-swap.

Note technique — Pourquoi deux appels à QuoteExactInputSingle donneraient
des prix différents sans trade réel ?
    Uniswap V3 utilise la liquidité concentrée : la liquidité est distribuée
    par tick de prix. Le Quoter simule la traversée réelle des ticks sans
    modifier l'état de la blockchain. Un ordre de 1 USDT consomme quasi-zéro
    liquidité → prix quasi-spot. Un ordre de 5M USDT traverse de nombreux
    ticks → prix d'exécution moyen dégradé. La différence est le price impact.
    Cependant, lire slot0() directement est plus précis et ne nécessite pas
    de second appel au Quoter.

Dépendances :
    pip install web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
  - Adams, H. et al., Uniswap v3 Core, Uniswap Labs, mars 2021
    https://uniswap.org/whitepaper-v3.pdf
  - Uniswap V3 Quoter V2 : 0x61fFE014bA17989E743c5F6cB21bF9697530B21e
  - Uniswap V3 Factory   : 0x1F98431c8aD98523631AE4a59f267346ea31F984
"""

import os
import json
import math
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── ADRESSES UNISWAP V3 (Ethereum mainnet) ───────────────────────────────────

UNISWAP_QUOTER_V2  = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
UNISWAP_FACTORY_V3 = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

FEE_TIERS = {
    100:   "0.01%  — stablecoins corrélés (ex. USDC/USDT)"
}

# ─── TOKENS DE RÉFÉRENCE (Ethereum mainnet) ───────────────────────────────────

TOKENS = {
    "USDC": {
        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "decimals": 6,
        "symbol": "USDC",
    },
    "USDT": {
        "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "decimals": 6,
        "symbol": "USDT",
    }
}

# ─── ABIs ─────────────────────────────────────────────────────────────────────

QUOTER_V2_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{
            "name": "params",
            "type": "tuple",
            "components": [
                {"name": "tokenIn",           "type": "address"},
                {"name": "tokenOut",          "type": "address"},
                {"name": "amountIn",          "type": "uint256"},
                {"name": "fee",               "type": "uint24"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ]
        }],
        "outputs": [
            {"name": "amountOut",               "type": "uint256"},
            {"name": "sqrtPriceX96After",       "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate",             "type": "uint256"},
        ]
    }
]

FACTORY_ABI = [
    {
        "name": "getPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee",    "type": "uint24"},
        ],
        "outputs": [{"name": "pool", "type": "address"}]
    }
]

# slot0 retourne notamment sqrtPriceX96 : le prix marginal courant du pool
POOL_ABI = [
    {
        "name": "slot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96",   "type": "uint160"},
            {"name": "tick",           "type": "int24"},
            {"name": "observationIdx", "type": "uint16"},
            {"name": "observationCardinality",        "type": "uint16"},
            {"name": "observationCardinalityNext",     "type": "uint16"},
            {"name": "feeProtocol",    "type": "uint8"},
            {"name": "unlocked",       "type": "bool"},
        ]
    },
    {
        "name": "liquidity",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint128"}]
    },
    {
        "name": "token0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}]
    },
    {
        "name": "token1",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}]
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# FONCTIONS PRINCIPALES
# ═══════════════════════════════════════════════════════════════════════════════

def get_pool_address(token_in: dict, token_out: dict, fee: int) -> str :
    """
    Récupère l'adresse du pool Uniswap V3 pour une paire donnée.
    Retourne Exception si le pool n'existe pas.
    """
    factory = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_FACTORY_V3),
        abi=FACTORY_ABI
    )
    pool_address = factory.functions.getPool(
        Web3.to_checksum_address(token_in["address"]),
        Web3.to_checksum_address(token_out["address"]),
        fee
    ).call()

    zero = "0x0000000000000000000000000000000000000000"
    if pool_address != zero :
        return pool_address 
    else :
        raise Exception("Pool do not exist")


def get_spot_price_from_slot0(
    pool_address: str,
    token_in: dict,
    token_out: dict,
) -> float :
    """
    Lit le prix spot marginal directement depuis slot0() du contrat de pool.

    POURQUOI CETTE MÉTHODE EST PLUS CORRECTE QUE DEUX APPELS AU QUOTER :
    slot0() expose sqrtPriceX96 — la racine carrée du prix au tick courant,
    encodée en virgule fixe Q64.96. C'est le prix marginal instantané,
    indépendant de toute simulation de swap. Il n'y a aucune approximation.

    Le prix réel est calculé depuis sqrtPriceX96 selon la formule :
        price = (sqrtPriceX96 / 2^96)^2

    Ce prix est exprimé en token1/token0 (ordre canonique Uniswap).
    On réordonne ensuite selon token_in/token_out.

    Args:
        pool_address : adresse du contrat de pool Uniswap V3
        token_in     : token vendu
        token_out    : token acheté

    Returns:
        Prix spot en unités de token_out par unité de token_in
    """
    pool = w3.eth.contract(
        address=Web3.to_checksum_address(pool_address),
        abi=POOL_ABI
    )

    try:
        slot0_data = pool.functions.slot0().call()
        sqrt_price_x96 = slot0_data[0]

        # token0 est le token avec l'adresse la plus petite (ordre Uniswap)
        token0_address = pool.functions.token0().call().lower()

        # Conversion sqrtPriceX96 → prix token1/token0
        # price_token1_per_token0 = (sqrtPriceX96 / 2^96)^2
        price_raw = (sqrt_price_x96 / (2**96)) ** 2

        # Ajustement des décimales
        # price_raw est en token1_smallest_unit / token0_smallest_unit
        decimals_token0 = (token_in["decimals"]
                           if token_in["address"].lower() == token0_address
                           else token_out["decimals"])
        decimals_token1 = (token_out["decimals"]
                           if token_in["address"].lower() == token0_address
                           else token_in["decimals"])

        price_adjusted = price_raw * (10**decimals_token0) / (10**decimals_token1)

        # Réorienter selon token_in → token_out
        # Si token_in = token0 → price est déjà token_out/token_in
        # Si token_in = token1 → inverser
        if token_in["address"].lower() == token0_address:
            return price_adjusted
        else:
            return 1.0 / price_adjusted if price_adjusted != 0 else None

    except Exception as e:
        raise Exception("Impossible to get Spotprice from Slot0")

def simuler_swap(
    token_in: dict,
    token_out: dict,
    montant_humain: float,
    fee: int
) -> dict :
    """
    Simule un swap Uniswap V3 via le Quoter V2 et calcule le price impact.

    MÉTHODE CORRIGÉE :
    - Le prix spot est lu depuis slot0() (prix marginal exact au tick courant)
    - Le prix d'exécution est obtenu via QuoteExactInputSingle (simulation
      de la traversée des ticks pour le montant réel)
    - Le price impact est la différence entre ces deux prix

    POURQUOI CE CALCUL EST VALIDE :
    Uniswap V3 utilise la liquidité concentrée : à chaque tick de prix,
    une quantité finie de liquidité est disponible. Un ordre important
    consomme la liquidité de plusieurs ticks successifs à des prix
    progressivement moins favorables. Le Quoter simule cette traversée
    sans modifier l'état de la blockchain. La différence entre le prix
    marginal (slot0) et le prix moyen d'exécution (Quoter) est le
    price impact réel de l'ordre.

    Args:
        token_in      : token vendu
        token_out     : token acheté
        montant_humain: montant en unités lisibles (ex. 1 000 000 USDC)
        fee           : frais du pool (100, 500, 3000 ou 10000)

    Returns:
        Dictionnaire avec les métriques d'exécution, ou Exception si pool inexistant
    """
    # Vérifier l'existence du pool
    pool_address = get_pool_address(token_in, token_out, fee)

    # ── Étape 1 : Prix spot marginal depuis slot0() ────────────────────────
    spot_price = get_spot_price_from_slot0(pool_address, token_in, token_out)

    # ── Étape 2 : Simulation du swap réel via Quoter V2 ───────────────────
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_QUOTER_V2),
        abi=QUOTER_V2_ABI
    )

    amount_in_raw = int(montant_humain * 10**token_in["decimals"])

    try:
        result = quoter.functions.quoteExactInputSingle({
            "tokenIn":           Web3.to_checksum_address(token_in["address"]),
            "tokenOut":          Web3.to_checksum_address(token_out["address"]),
            "amountIn":          amount_in_raw,
            "fee":               fee,
            "sqrtPriceLimitX96": 0,
        }).call()

        amount_out_raw  = result[0]
        sqrt_after      = result[1]   # prix après le swap simulé
        ticks_crossed   = result[2]   # nombre de ticks traversés
        gas_estimate    = result[3]

        amount_out_humain = amount_out_raw / 10**token_out["decimals"]
        prix_execution    = amount_out_humain / montant_humain

    except Exception as e:
        raise Exception ("Error calling QuoterV2 - quoteExcactInputSingle")

    # ── Étape 3 : Calcul du price impact ──────────────────────────────────
    # Price impact = (spot - execution) / spot × 100
    # Toujours positif : l'exécution est toujours moins favorable que le spot
    price_impact_pct = (spot_price - prix_execution) / spot_price * 100

    price_impact_bis = (prix_execution - sqrt_after)/ prix_execution *100

    # Prix théorique sans aucun impact (spot pur)
    montant_out_theorique = montant_humain * spot_price

    # Perte en valeur absolue due au price impact
    perte_price_impact = montant_out_theorique - amount_out_humain

    # Frais de pool en unités de token_out
    frais_pool_humain = montant_humain * (fee / 1_000_000) * prix_execution

    # Coût total = price impact + frais de pool (en % du notionnel)
    cout_total_pct = price_impact_pct + (fee / 1_000_000 * 100)

    return {
        "fee_tier":            fee,
        "fee_label":           FEE_TIERS[fee],
        "pool_address":        pool_address,
        "amount_in":           montant_humain,
        "amount_out":          amount_out_humain,
        "montant_out_theorique": montant_out_theorique,
        "spot_price":          spot_price,
        "prix_execution":      prix_execution,
        "price_impact_pct":    round(price_impact_pct, 6),
        "perte_price_impact":  round(perte_price_impact, 4),
        "frais_pool":          round(frais_pool_humain, 4),
        "cout_total_pct":      round(cout_total_pct, 6),
        "ticks_traverses":     ticks_crossed,
        "gas_estimate":        gas_estimate,
    }


def analyser_routes(
    symbol_in: str,
    symbol_out: str,
    montant: float,
    slippage_max_pct: float = 0.30
) -> dict:
    """
    Analyse toutes les routes disponibles entre deux tokens sur Uniswap V3
    et recommande la meilleure pour un institutionnel.

    Pour chaque tranche de frais (0.01%, 0.05%, 0.3%, 1%) :
    - Obtient le prix spot depuis slot0()
    - Simule le swap via Quoter V2
    - Calcule le price impact exact
    - Compare le coût total (price impact + frais)

    Args:
        symbol_in        : symbole du token à vendre (ex. "USDT")
        symbol_out       : symbole du token à acheter (ex. "USDC")
        montant          : montant en unités lisibles
        slippage_max_pct : tolérance maximum au price impact (défaut : 0.30%)

    Returns:
        Dictionnaire avec les résultats par fee tier et la recommandation
    """
    token_in  = TOKENS[symbol_in]
    token_out = TOKENS[symbol_out]

    resultats = {}
    meilleur  = None
    meilleur_cout_total = float('inf')

    print(f"\n{'─'*62}")
    print(f"ANALYSE DE ROUTING — {symbol_in} → {symbol_out}")
    print(f"Montant         : {montant:>18,.2f} {symbol_in}")
    print(f"Seuil max       : {slippage_max_pct}% de price impact")
    print(f"{'─'*62}")
    print(f"{'Pool':>8}  {'Prix spot':>12}  {'Prix exec.':>12}  "
          f"{'Impact':>8}  {'Frais':>7}  {'Coût total':>10}  {'Ticks':>6}")
    print(f"{'─'*62}")

    for fee in FEE_TIERS:
        r = simuler_swap(token_in, token_out, montant, fee)

        if r is None:
            print(f"  {fee/10000:.2f}%  ❌  Pool inexistant ou liquidité insuffisante")
            continue

        statut = ("🚨" if r["price_impact_pct"] > slippage_max_pct
                  else "⚠️ " if r["price_impact_pct"] > 0.10
                  else "✅")

        print(f"  {fee/10000:.2f}%   "
              f"{r['spot_price']:>12.6f}  "
              f"{r['prix_execution']:>12.6f}  "
              f"{r['price_impact_pct']:>7.4f}%  "
              f"{r['frais_pool']:>9.2f}  "
              f"{r['cout_total_pct']:>9.4f}%  "
              f"{r['ticks_traverses']:>6}  {statut}")

        resultats[fee] = r

        if r["cout_total_pct"] < meilleur_cout_total:
            meilleur_cout_total = r["cout_total_pct"]
            meilleur = fee

    # ── Recommandation finale ──────────────────────────────────────────────────
    print(f"{'─'*62}")

    if meilleur is None:
        print("❌ AUCUN POOL DISPONIBLE — swap impossible dans les conditions actuelles")
        recommandation = "ANNULER — aucune liquidité disponible"
        slippage_config = None

    else:
        r = resultats[meilleur]

        # Paramètre slippage à configurer dans la transaction :
        # amountOutMinimum = amount_out_simulé × (1 - tolerance)
        # On prend 1.5× le price impact simulé comme marge de sécurité
        tolerance = max(r["price_impact_pct"] * 1.5, 0.05) / 100
        amount_out_minimum = r["amount_out"] * (1 - tolerance)
        slippage_config    = round(tolerance * 100, 4)

        if r["price_impact_pct"] > slippage_max_pct:
            nb_tranches = max(2, math.ceil(r["price_impact_pct"] / slippage_max_pct))
            print(f"\n⚠️  RECOMMANDATION : FRACTIONNER EN {nb_tranches} TRANCHES")
            print(f"   Price impact ({r['price_impact_pct']:.4f}%) > seuil ({slippage_max_pct}%)")
            print(f"   Montant par tranche : {montant/nb_tranches:,.2f} {symbol_in}")
            recommandation = (f"FRACTIONNER ×{nb_tranches} — "
                              f"price impact {r['price_impact_pct']:.4f}% > seuil")
        else:
            print(f"\n✅ RECOMMANDATION : POOL {meilleur/10000:.2f}%")
            print(f"   Coût total estimé       : {meilleur_cout_total:.4f}%")
            print(f"   Montant reçu simulé     : {r['amount_out']:,.4f} {symbol_out}")
            print(f"   Perte due au price impact: {r['perte_price_impact']:,.4f} {symbol_out}")
            print(f"   Paramètre slippage tx   : {slippage_config}%")
            print(f"   amountOutMinimum à fixer : {amount_out_minimum:,.4f} {symbol_out}")
            recommandation = (f"EXÉCUTER pool {meilleur/10000:.2f}% — "
                              f"coût total {meilleur_cout_total:.4f}%")

    return {
        "paire":           f"{symbol_in}/{symbol_out}",
        "montant":         montant,
        "timestamp":       datetime.utcnow().isoformat(),
        "resultats":       {str(k): v for k, v in resultats.items()},
        "meilleur_pool":   meilleur,
        "recommandation":  recommandation,
        "slippage_config": slippage_config,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("SCRIPT 3 — PRICE IMPACT & ROUTING UNISWAP V3")
    print("Optimisation pré-trade avant dépôt sur Morpho Blue")
    print("Section IV.1 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*62)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")

    resultats_globaux = []

    # ── CAS 1 : USDT → USDC (stablecoins) ────────────────────────────────────
    print("\n" + "═"*62)
    print("CAS 1 : USDT → USDC (avant dépôt Morpho USDC/wstETH)")
    print("═"*62)
    res1 = analyser_routes("USDT", "USDC", 5_000_000, slippage_max_pct=0.10)
    resultats_globaux.append(res1)

    # ── Export JSON ───────────────────────────────────────────────────────────
    output_file = "pre_trade_routing_report.json"
    with open(output_file, "w") as f:
        json.dump(resultats_globaux, f, indent=2, default=str)

    print(f"\n{'━'*62}")
    print(f"💾 Rapport exporté : {output_file}")
    print(f"\nRÉCAPITULATIF DES RECOMMANDATIONS :")
    for r in resultats_globaux:
        print(f"  {r['paire']:<20} {r['montant']:>15,.0f}  →  {r['recommandation']}")
    print("━"*62)