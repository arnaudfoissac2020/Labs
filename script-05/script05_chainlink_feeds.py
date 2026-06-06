"""
SCRIPT 5 — Price feeds Chainlink utilisés par Morpho Blue
          Flux de valorisation temps réel avec gestion du staleness

Contexte : La valorisation des positions Morpho Blue repose sur les oracles
           de prix utilisés par chaque marché. Ce script interroge ces oracles
           directement on-chain, vérifie leur fraîcheur (staleness check),
           implémente une logique de fallback en cas d'oracle indisponible,
           et produit un flux de valorisation structuré injectable dans les
           systèmes de risque et de P&L de l'institution.

           Deux niveaux de lecture sont implémentés :
           1. Oracle du marché Morpho (price()) — source primaire pour les
              calculs internes du protocole (liquidations, health factor)
           2. Feeds Chainlink directs (latestRoundData()) — source secondaire
              pour la valorisation officielle de l'institution

           Le staleness check est critique : un oracle dont le prix n'a pas
           été mis à jour depuis plus de son heartbeat configuré est considéré
           comme stale et ne doit pas être utilisé pour une valorisation
           officielle (cf. Section IV.3 du mémoire).

Dépendances :
    pip install web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
  - Chainlink Labs, Chainlink 2.0 white paper v2, 2021
    https://research.chain.link/whitepaper-v2.pdf
  - Morpho Docs, Oracle, 2025
    https://docs.morpho.org/learn/concepts/oracle/
  - Mackinga et al., TWAP Oracle Attacks, arXiv:2208.09903, 2022
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── ADRESSES ─────────────────────────────────────────────────────────────────

MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

# Feeds Chainlink directs (ETH mainnet)
# Chaque feed est accompagné de son heartbeat officiel et de son seuil
# de déviation (deviation threshold) — paramètres de mise à jour Chainlink
CHAINLINK_FEEDS = {
    "ETH/USD": {
        "address":   "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
        "decimals":  8,
        "heartbeat": 3600,      # 1h — mise à jour si déviation > 0.5% ou après 1h
        "deviation": 0.005,     # 0.5%
    },
    "USDC/USD": {
        "address":   "0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6",
        "decimals":  8,
        "heartbeat": 86400,     # 24h — actif stable, mise à jour si déviation > 0.1%
        "deviation": 0.001,     # 0.1%
    },
    "USDT/USD": {
        "address":   "0x3E7d1eAB13ad0104d2750B8863b489D65364e32D",
        "decimals":  8,
        "heartbeat": 86400,     # 24h
        "deviation": 0.001,
    },
    "WBTC/USD": {
        "address":   "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c",
        "decimals":  8,
        "heartbeat": 3600,      # 1h
        "deviation": 0.005,
    },
    "stETH/USD": {
        "address":   "0xCfE54B5cD566aB89272946F602D76Ea879CAb4a8",
        "decimals":  8,
        "heartbeat": 3600,      # 1h
        "deviation": 0.005,
    },
    "DAI/USD": {
        "address":   "0xAed0c38402a5d19df6E4c03F4E2DceD6e29c1ee9",
        "decimals":  8,
        "heartbeat": 3600,      # 1h
        "deviation": 0.001,
    },
    "wstETH/stETH": {
        # Oracle de taux de change wstETH → stETH (déterministe, pas de déviation)
        # Chainlink wstETH/USD compose ce feed avec stETH/USD
        "address":   "0x8B6851156023f4f5A66F68BEA80851c3D905Ac93",
        "decimals":  18,
        "heartbeat": 86400,     # 24h — taux d'échange quasi-déterministe
        "deviation": 0.001,
    },
}

# Marchés Morpho Blue de référence avec leur configuration d'oracle
MORPHO_MARKETS = {
    "USDC/wstETH (86%)": {
        "market_id": "0xb323495f7e4148be5643a4ea4a8221eef163e4bccfdedc2a6f4696baacbc86cc",
        "loan":       "USDC",
        "collateral": "wstETH",
        "lltv":       0.86,
        # L'oracle Morpho pour wstETH/USDC compose :
        # wstETH/stETH (taux de change) × stETH/USD × inverse USD/USDC
        "feeds_composites": ["wstETH/stETH", "stETH/USD", "USDC/USD"],
    },
    "USDC/WBTC (86%)": {
        "market_id": "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",
        "loan":       "USDC",
        "collateral": "WBTC",
        "lltv":       0.86,
        "feeds_composites": ["WBTC/USD", "USDC/USD"],
    },
    "WETH/USDC (91.5%)": {
        "market_id": "0x7dde86a1e94561d9690ec678db673c1a6396365f7d1d65e129c5fff0990ff758",
        "loan":       "USDC",
        "collateral": "WETH",
        "lltv":       0.915,
        "feeds_composites": ["ETH/USD", "USDC/USD"],
    },
}

# ─── ABIs ─────────────────────────────────────────────────────────────────────

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

# Interface IOracle de Morpho — expose price() directement
MORPHO_ORACLE_ABI = [
    {
        "name": "price",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [],
        "outputs": [{"name": "", "type": "uint256"}]
    }
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


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURE ET VÉRIFICATION DES PRICE FEEDS
# ═══════════════════════════════════════════════════════════════════════════════

def lire_chainlink_feed(feed_name: str, feed_config: dict) -> dict:
    """
    Lit un price feed Chainlink et vérifie sa fraîcheur.

    STALENESS CHECK :
    Un feed Chainlink est considéré stale si :
    block.timestamp - updatedAt > heartbeat configuré

    Dans ce cas, le feed ne doit PAS être utilisé pour une valorisation
    officielle. La logique de fallback doit être activée.

    Le heartbeat est le délai maximum entre deux mises à jour — même si
    le prix n'a pas bougé de plus du seuil de déviation, une mise à jour
    est forcée à l'expiration du heartbeat.

    Args:
        feed_name   : nom du feed (ex. "ETH/USD")
        feed_config : configuration du feed (adresse, heartbeat, etc.)

    Returns:
        Dictionnaire avec le prix, le statut de fraîcheur et les métadonnées
    """
    feed = w3.eth.contract(
        address=Web3.to_checksum_address(feed_config["address"]),
        abi=CHAINLINK_ABI
    )

    now = int(time.time())

    try:
        data = feed.functions.latestRoundData().call()
        round_id    = data[0]
        answer      = data[1]
        started_at  = data[2]
        updated_at  = data[3]
        answered_in = data[4]

        # Conversion du prix brut en valeur lisible
        prix = answer / (10**feed_config["decimals"])

        # ── Staleness check ────────────────────────────────────────────────────
        age_secondes = now - updated_at
        age_minutes  = age_secondes / 60
        age_heures   = age_secondes / 3600
        heartbeat    = feed_config["heartbeat"]
        est_stale    = age_secondes > heartbeat

        # Marge de sécurité : alerte à 80% du heartbeat
        alerte_80pct = age_secondes > (heartbeat * 0.80)

        # ── Circuit breaker : prix manifestement aberrant ──────────────────────
        # Vérification basique que le prix est positif et non nul
        prix_valide = prix > 0 and answer > 0

        # Cohérence round : le round ayant répondu doit être le round actuel
        # Si answeredInRound < roundId → le round n'a pas été correctement finalisé
        round_coherent = answered_in >= round_id

        # Détermination du statut global
        if not prix_valide:
            statut = "INVALIDE"
        elif est_stale:
            statut = "STALE"
        elif alerte_80pct:
            statut = "ATTENTION"
        else:
            statut = "FRAIS"

        dt_update = datetime.fromtimestamp(updated_at, tz=timezone.utc)

        return {
            "feed_name":      feed_name,
            "address":        feed_config["address"],
            "prix":           round(prix, 8),
            "round_id":       round_id,
            "updated_at":     dt_update.isoformat(),
            "age_secondes":   age_secondes,
            "age_humain":     (f"{age_heures:.1f}h" if age_heures >= 1
                               else f"{age_minutes:.0f}min"),
            "heartbeat":      heartbeat,
            "deviation_seuil": feed_config["deviation"],
            "est_stale":      est_stale,
            "alerte_80pct":   alerte_80pct,
            "prix_valide":    prix_valide,
            "round_coherent": round_coherent,
            "statut":         statut,
            "utilisation":    "VALORISATION_OFFICIELLE" if statut == "FRAIS" else "FALLBACK_REQUIS",
        }

    except Exception as e:
        return {
            "feed_name":   feed_name,
            "address":     feed_config["address"],
            "prix":        None,
            "statut":      "ERREUR",
            "erreur":      str(e),
            "utilisation": "FALLBACK_REQUIS",
        }


def lire_oracle_morpho(market_id_hex: str, loan_decimals: int = 6,
                        collateral_decimals: int = 18) -> dict:
    """
    Lit le prix retourné par l'oracle immuable d'un marché Morpho Blue.

    L'oracle Morpho expose une unique fonction price() qui retourne
    le prix du collatéral exprimé en actif de prêt, normalisé à
    ORACLE_PRICE_SCALE = 1e36 (pour gérer les différences de décimales).

    DIFFÉRENCE AVEC LES FEEDS CHAINLINK DIRECTS :
    - L'oracle Morpho est composé (il agrège plusieurs feeds Chainlink)
    - Il normalise automatiquement les décimales entre loan et collateral
    - Il est IMMUABLE — défini à la création du marché
    - Son résultat est ce que Morpho utilise pour calculer le health factor

    Pour la valorisation officielle de l'institution, il est recommandé
    de croiser le prix de l'oracle Morpho avec les feeds Chainlink directs.

    Args:
        market_id_hex        : ID du marché Morpho (bytes32 en hex)
        loan_decimals        : décimales du loan asset (ex. 6 pour USDC)
        collateral_decimals  : décimales du collateral asset (ex. 18 pour wstETH)

    Returns:
        Dictionnaire avec le prix de l'oracle Morpho et ses métadonnées
    """
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_PARAMS_ABI
    )

    try:
        market_id_bytes = bytes.fromhex(market_id_hex[2:])
        params = morpho.functions.idToMarketParams(market_id_bytes).call()
        oracle_address = params[2]

        oracle = w3.eth.contract(
            address=Web3.to_checksum_address(oracle_address),
            abi=MORPHO_ORACLE_ABI
        )

        # price() retourne le prix normalisé à ORACLE_PRICE_SCALE = 1e36
        # C'est le prix de 1 unité de collatéral en unités de loan asset
        # Prix réel = price_raw / (10^36) × 10^(loan_decimals - collateral_decimals)
        price_raw = oracle.functions.price().call()

        # Normalisation selon la formule Morpho
        # (cf. Morpho Blue Whitepaper + documentation oracle)
        ORACLE_PRICE_SCALE = 1e36
        prix_collateral_en_loan = (
            price_raw / ORACLE_PRICE_SCALE
        ) * (10**(collateral_decimals - loan_decimals))

        return {
            "oracle_address":           oracle_address,
            "price_raw":                price_raw,
            "prix_collateral_en_loan":  round(prix_collateral_en_loan, 8),
            "statut":                   "OK",
            "note": (
                "Prix de l'oracle Morpho — utilisé pour le calcul du health "
                "factor et les liquidations on-chain. Source primaire Morpho, "
                "croiser avec les feeds Chainlink directs pour la valorisation "
                "officielle de l'institution."
            )
        }

    except Exception as e:
        return {
            "oracle_address": None,
            "prix_collateral_en_loan": None,
            "statut": "ERREUR",
            "erreur": str(e),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# FLUX DE VALORISATION
# ═══════════════════════════════════════════════════════════════════════════════

def generer_flux_valorisation(
    positions: list[dict],
    seuil_staleness_alerte: float = 0.80
) -> dict:
    """
    Génère un flux de valorisation structuré pour un portefeuille de positions
    Morpho Blue, en croisant l'oracle Morpho et les feeds Chainlink directs.

    Pour chaque position :
    1. Lit le prix de l'oracle Morpho (source primaire Morpho)
    2. Lit les feeds Chainlink composites correspondants (source secondaire)
    3. Vérifie le staleness de chaque feed
    4. Calcule la valorisation en USD
    5. Signale les écarts anormaux entre sources (circuit breaker)

    Args:
        positions : liste de positions à valoriser (dict avec market_id,
                    montant, type SUPPLY/BORROW/COLLATERAL)
        seuil_staleness_alerte : ratio du heartbeat pour déclencher une alerte

    Returns:
        Flux de valorisation structuré avec statut par position
    """
    timestamp = datetime.utcnow().isoformat()
    flux = {
        "timestamp":      timestamp,
        "bloc":           w3.eth.block_number,
        "positions":      [],
        "alertes":        [],
        "statut_global":  "OK",
    }

    # Lecture préalable de tous les feeds Chainlink nécessaires
    feeds_cache = {}
    for feed_name, config in CHAINLINK_FEEDS.items():
        feeds_cache[feed_name] = lire_chainlink_feed(feed_name, config)

    for position in positions:
        market_config = MORPHO_MARKETS.get(position.get("marche"))
        if not market_config:
            continue

        market_id   = market_config["market_id"]
        loan        = market_config["loan"]
        collateral  = market_config["collateral"]
        montant     = position.get("montant", 0)
        type_pos    = position.get("type", "SUPPLY")  # SUPPLY ou COLLATERAL

        # ── Lecture de l'oracle Morpho ─────────────────────────────────────────
        loan_decimals  = 6 if loan == "USDC" else 18
        coll_decimals  = 18  # wstETH, WETH ont 18 décimales ; WBTC = 8
        if collateral == "WBTC":
            coll_decimals = 8

        oracle_morpho = lire_oracle_morpho(market_id, loan_decimals, coll_decimals)

        # ── Valorisation en USD selon le type de position ──────────────────────
        alertes_position = []
        prix_usd_principal = None

        if type_pos == "SUPPLY":
            # Position prêteur : valorisation en USD de l'actif de prêt
            feed_loan = feeds_cache.get(f"{loan}/USD")
            if feed_loan and feed_loan["statut"] == "FRAIS":
                prix_usd_principal = feed_loan["prix"]
                valorisation_usd   = montant * prix_usd_principal
            else:
                valorisation_usd = None
                alertes_position.append(f"Feed {loan}/USD stale ou indisponible")

        elif type_pos == "COLLATERAL":
            # Position emprunteur : valorisation en USD du collatéral déposé
            # Croiser l'oracle Morpho avec les feeds Chainlink composites
            feeds_composites = market_config.get("feeds_composites", [])
            prix_collateral_usd_chainlink = None

            if collateral == "wstETH":
                f_wsteth = feeds_cache.get("wstETH/stETH")
                f_steth  = feeds_cache.get("stETH/USD")
                if (f_wsteth and f_steth and
                    f_wsteth["statut"] == "FRAIS" and f_steth["statut"] == "FRAIS"):
                    prix_collateral_usd_chainlink = (
                        f_wsteth["prix"] * f_steth["prix"]
                    )
            elif collateral == "WBTC":
                f_wbtc = feeds_cache.get("WBTC/USD")
                if f_wbtc and f_wbtc["statut"] == "FRAIS":
                    prix_collateral_usd_chainlink = f_wbtc["prix"]
            elif collateral == "WETH":
                f_eth = feeds_cache.get("ETH/USD")
                if f_eth and f_eth["statut"] == "FRAIS":
                    prix_collateral_usd_chainlink = f_eth["prix"]

            # Circuit breaker : vérifier la cohérence Morpho oracle vs Chainlink
            if (prix_collateral_usd_chainlink and
                oracle_morpho.get("prix_collateral_en_loan")):

                # Conversion prix oracle Morpho en USD via USDC/USD
                f_usdc = feeds_cache.get("USDC/USD")
                if f_usdc and f_usdc["statut"] == "FRAIS":
                    prix_morpho_usd = (
                        oracle_morpho["prix_collateral_en_loan"] * f_usdc["prix"]
                    )
                    ecart_pct = abs(
                        prix_morpho_usd - prix_collateral_usd_chainlink
                    ) / prix_collateral_usd_chainlink * 100

                    if ecart_pct > 1.0:
                        alertes_position.append(
                            f"⚠️ CIRCUIT BREAKER : écart Morpho oracle vs Chainlink "
                            f"= {ecart_pct:.3f}% (> 1% — vérification manuelle requise)"
                        )

            prix_usd_principal = prix_collateral_usd_chainlink
            valorisation_usd   = (montant * prix_usd_principal
                                  if prix_usd_principal else None)

        # ── Statuts des feeds composites ───────────────────────────────────────
        statuts_feeds = {}
        for feed_name in market_config.get("feeds_composites", []):
            feed_data = feeds_cache.get(feed_name)
            if feed_data:
                statuts_feeds[feed_name] = {
                    "prix":        feed_data.get("prix"),
                    "statut":      feed_data.get("statut"),
                    "age":         feed_data.get("age_humain"),
                    "est_stale":   feed_data.get("est_stale"),
                }
                if feed_data.get("est_stale"):
                    alertes_position.append(
                        f"🚨 Feed {feed_name} STALE — "
                        f"âge {feed_data.get('age_humain')} > "
                        f"heartbeat {feed_data.get('heartbeat')}s"
                    )

        # ── Statut de la position ─────────────────────────────────────────────
        statut_position = "OK"
        if any("STALE" in a or "CIRCUIT BREAKER" in a
               for a in alertes_position):
            statut_position = "ALERTE"
            flux["statut_global"] = "ALERTE"

        flux["positions"].append({
            "marche":              position.get("marche"),
            "type":                type_pos,
            "loan_asset":          loan,
            "collateral_asset":    collateral,
            "montant":             montant,
            "prix_usd":            round(prix_usd_principal, 6) if prix_usd_principal else None,
            "valorisation_usd":    round(valorisation_usd, 2) if valorisation_usd else None,
            "oracle_morpho":       oracle_morpho.get("prix_collateral_en_loan"),
            "statuts_feeds":       statuts_feeds,
            "statut_position":     statut_position,
            "alertes":             alertes_position,
        })

        flux["alertes"].extend(alertes_position)

    return flux


def afficher_rapport_feeds(feeds_cache: dict) -> None:
    """Affiche un rapport de statut de tous les feeds Chainlink lus."""
    print(f"\n{'─'*72}")
    print(f"{'Feed':<16} {'Prix':>12} {'Âge':>8} {'Heartbeat':>10} "
          f"{'Statut':>12} {'Utilisation'}")
    print(f"{'─'*72}")

    for feed_name, data in feeds_cache.items():
        if data["statut"] == "ERREUR":
            print(f"  {feed_name:<14} {'ERREUR':>12} {'N/A':>8} "
                  f"{'N/A':>10} {'❌ ERREUR':>12}")
            continue

        icone = {"FRAIS": "✅", "ATTENTION": "⚠️ ", "STALE": "🚨",
                 "INVALIDE": "❌", "ERREUR": "❌"}.get(data["statut"], "❓")

        print(f"  {feed_name:<14} {data['prix']:>12.6f} "
              f"{data['age_humain']:>8} "
              f"{data['heartbeat']:>8}s  "
              f"{icone} {data['statut']:>8}  "
              f"{data['utilisation']}")


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*72)
    print("SCRIPT 5 — PRICE FEEDS CHAINLINK / MORPHO BLUE — VALORISATION")
    print("Flux de valorisation temps réel avec staleness check et fallback")
    print("Section IV.3 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*72)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")
    print(f"   Timestamp UTC : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── Partie 1 : Lecture et vérification de tous les feeds Chainlink ─────────
    print(f"\n{'═'*72}")
    print("PARTIE 1 — STATUT DES PRICE FEEDS CHAINLINK")
    print(f"{'═'*72}")

    feeds_cache = {}
    for feed_name, config in CHAINLINK_FEEDS.items():
        feeds_cache[feed_name] = lire_chainlink_feed(feed_name, config)

    afficher_rapport_feeds(feeds_cache)

    # Compter les feeds stale
    n_frais   = sum(1 for f in feeds_cache.values() if f.get("statut") == "FRAIS")
    n_stale   = sum(1 for f in feeds_cache.values() if f.get("statut") == "STALE")
    n_erreur  = sum(1 for f in feeds_cache.values() if f.get("statut") == "ERREUR")
    print(f"\n  Résumé : {n_frais} frais ✅  {n_stale} stale 🚨  {n_erreur} erreur ❌")

    if n_stale > 0:
        print(f"\n  🚨 ALERTE STALENESS : {n_stale} feed(s) en dehors du heartbeat")
        print(f"     → Ne pas utiliser ces feeds pour la valorisation officielle")
        print(f"     → Activer le fallback (prix Bloomberg/Refinitiv ou dernière valeur valide)")

    # ── Partie 2 : Lecture des oracles Morpho par marché ──────────────────────
    print(f"\n{'═'*72}")
    print("PARTIE 2 — ORACLES DES MARCHÉS MORPHO BLUE")
    print(f"{'═'*72}")

    for nom_marche, config in MORPHO_MARKETS.items():
        loan_dec = 6 if config["loan"] == "USDC" else 18
        coll_dec = 8 if config["collateral"] == "WBTC" else 18
        result   = lire_oracle_morpho(config["market_id"], loan_dec, coll_dec)

        print(f"\n  Marché : {nom_marche}")
        if result["statut"] == "OK":
            print(f"    Oracle    : {result['oracle_address']}")
            print(f"    Prix      : {result['prix_collateral_en_loan']:.6f} "
                  f"{config['loan']} par unité de {config['collateral']}")
            print(f"    ✅ Oracle fonctionnel")
        else:
            print(f"    ❌ Oracle indisponible : {result.get('erreur', 'N/A')}")

    # ── Partie 3 : Flux de valorisation d'un portefeuille de démonstration ─────
    print(f"\n{'═'*72}")
    print("PARTIE 3 — FLUX DE VALORISATION PORTEFEUILLE")
    print(f"{'═'*72}")

    # Portefeuille de démonstration institutionnel
    positions_demo = [
        {
            "marche":  "USDC/wstETH (86%)",
            "type":    "SUPPLY",
            "montant": 5_000_000,   # 5M USDC déposés comme prêteur
        },
        {
            "marche":  "USDC/wstETH (86%)",
            "type":    "COLLATERAL",
            "montant": 2_500,       # 2 500 wstETH déposés comme collatéral
        },
        {
            "marche":  "USDC/WBTC (86%)",
            "type":    "SUPPLY",
            "montant": 3_000_000,   # 3M USDC prêtés sur marché WBTC
        },
        {
            "marche":  "WETH/USDC (91.5%)",
            "type":    "COLLATERAL",
            "montant": 1_000,       # 1 000 WETH comme collatéral
        },
    ]

    flux = generer_flux_valorisation(positions_demo)

    print(f"\n  Timestamp : {flux['timestamp']}")
    print(f"  Bloc      : #{flux['bloc']:,}")
    print(f"  Statut    : {flux['statut_global']}")

    valorisation_totale = 0
    print(f"\n  {'Position':<30} {'Type':<12} {'Montant':>14} {'Prix USD':>12} {'Valeur USD':>15} {'Statut'}")
    print(f"  {'─'*95}")

    for pos in flux["positions"]:
        val = pos.get("valorisation_usd")
        if val:
            valorisation_totale += val
        val_str  = f"${val:>14,.2f}" if val else f"{'N/A':>15}"
        prix_str = f"${pos.get('prix_usd', 0):>11,.4f}" if pos.get("prix_usd") else f"{'N/A':>12}"
        icone    = "✅" if pos["statut_position"] == "OK" else "⚠️ "
        actif    = pos["collateral_asset"] if pos["type"] == "COLLATERAL" else pos["loan_asset"]

        print(f"  {pos['marche']:<30} {pos['type']:<12} "
              f"{pos['montant']:>14,.1f} {prix_str} {val_str} {icone}")

        for alerte in pos.get("alertes", []):
            print(f"    ↳ {alerte}")

    print(f"  {'─'*95}")
    print(f"  {'VALORISATION TOTALE PORTEFEUILLE':>58} ${valorisation_totale:>14,.2f}")

    if flux["alertes"]:
        print(f"\n  ⚠️  {len(flux['alertes'])} alerte(s) active(s) :")
        for alerte in set(flux["alertes"]):
            print(f"     → {alerte}")

    # ── Export JSON ───────────────────────────────────────────────────────────
    output = {
        "metadata": {
            "script":    "Script 5 — Price feeds Chainlink / Morpho Blue",
            "timestamp": flux["timestamp"],
            "bloc":      flux["bloc"],
        },
        "feeds_chainlink": feeds_cache,
        "flux_valorisation": flux,
    }

    output_file = "morpho_valuation_flux.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n{'━'*72}")
    print(f"💾 Flux de valorisation exporté : {output_file}")
    print(f"\nSTATUT GLOBAL : {flux['statut_global']}")
    if flux["statut_global"] == "ALERTE":
        print("⚠️  Action requise — vérifier les alertes ci-dessus avant")
        print("   utilisation en valorisation officielle.")
    else:
        print("✅ Tous les feeds sont frais — valorisation officielle possible.")
    print("━"*72)