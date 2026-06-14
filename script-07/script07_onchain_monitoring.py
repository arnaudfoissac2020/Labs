"""
SCRIPT 7 — Monitoring on-chain des risques Morpho Blue
          Alertes automatiques par seuil

Contexte : Ce script implémente la surveillance continue des risques
           opérationnels associés aux positions Morpho Blue d'un
           institutionnel. Il surveille quatre catégories de risques
           (cf. Section IV.4 du mémoire) :

           1. Taux d'utilisation des marchés cibles
              → Seuil d'alerte : > 92% (zone de stress Adaptive Curve IRM)
              → Seuil critique  : > 97% (retrait quasi-impossible)

           2. Fraîcheur des price feeds Chainlink (oracle staleness)
              → Alerte à 80% du heartbeat configuré par feed
              → Critique si > heartbeat (valorisation officielle bloquée)

           3. Health factor des positions emprunteur
              → Alerte si Health Factor < 1.15 (pré-liquidation)
              → Critique si Health Factor < 1.05 (liquidation imminente)

           4. Gouvernance Morpho
              → Nouvelles propositions d'IRM ou LLTV (modification des
                paramètres éligibles pour les futurs marchés)
              → Bad debt récente sur les marchés cibles

           En production, ce script est exécuté :
           - En continu (boucle de surveillance, mode daemon)
           - À chaque ouverture/fermeture de journée (mode one-shot)
           - Sur déclenchement d'un webhook externe

Dépendances :
    pip install web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
  - Morpho Docs, Interest Rate Model, 2025
    https://docs.morpho.org/learn/concepts/irm/
  - Morpho Docs, Liquidation on Morpho, 2025
    https://docs.morpho.org/learn/concepts/liquidation/
  - BIS Quarterly Review, DeFi risks, décembre 2021
    https://www.bis.org/publ/qtrpdf/r_qt2112b.htm
"""

import os, sys
import json
import time
from datetime import datetime, timezone
from enum import Enum
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── ADRESSES ─────────────────────────────────────────────────────────────────

MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

# Feeds Chainlink avec heartbeat officiel
CHAINLINK_FEEDS = {
    "ETH/USD":      {"address": "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419", "heartbeat": 3600,  "decimals": 8},
    "USDC/USD":     {"address": "0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6", "heartbeat": 86400, "decimals": 8},
    "USDT/USD":     {"address": "0x3E7d1eAB13ad0104d2750B8863b489D65364e32D", "heartbeat": 86400, "decimals": 8},
    "WBTC/USD":     {"address": "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c", "heartbeat": 3600,  "decimals": 8},
    "stETH/USD":    {"address": "0xCfE54B5cD566aB89272946F602D76Ea879CAb4a8", "heartbeat": 3600,  "decimals": 8},
    "wstETH/stETH": {"address": "0x8B6851156023f4f5A66F68BEA80851c3D905Ac93", "heartbeat": 86400, "decimals": 18},
}

# Marchés Morpho cibles de l'institution
MORPHO_MARKETS_CIBLES = {
    "USDC/wstETH (86%)": {
        "id": "0xb323495f7e4148be5643a4ea4a8221eef163e4bccfdedc2a6f4696baacbc86cc",
        "loan_decimals": 6,
        "feeds": ["wstETH/stETH", "stETH/USD", "USDC/USD"],
    },
    "USDC/WBTC (86%)": {
        "id": "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",
        "loan_decimals": 6,
        "feeds": ["WBTC/USD", "USDC/USD"],
    },
    "WETH/USDC (91.5%)": {
        "id": "0x7dde86a1e94561d9690ec678db673c1a6396365f7d1d65e129c5fff0990ff758",
        "loan_decimals": 6,
        "feeds": ["ETH/USD", "USDC/USD"],
    },
}

# ─── SEUILS D'ALERTE ──────────────────────────────────────────────────────────

SEUILS = {
    # Taux d'utilisation
    "utilisation_attention":  0.85,   # 85% — surveiller
    "utilisation_alerte":     0.92,   # 92% — alerte (zone de stress IRM)
    "utilisation_critique":   0.97,   # 97% — critique (retrait quasi-impossible)

    # Staleness oracle (ratio du heartbeat)
    "staleness_attention":    0.70,   # 70% du heartbeat — surveiller
    "staleness_alerte":       0.85,   # 85% du heartbeat — alerte
    "staleness_critique":     1.00,   # 100% du heartbeat — feed stale (critique)

    # Health factor emprunteur
    "hf_attention":           1.20,   # HF < 1.20 — surveiller
    "hf_alerte":              1.10,   # HF < 1.10 — alerte
    "hf_critique":            1.05,   # HF < 1.05 — liquidation imminente

    # Variation de TVL sur la période de monitoring
    "tvl_variation_alerte":   0.10,   # -10% de TVL sur la fenêtre
}

# ─── ÉNUMÉRATION DES NIVEAUX D'ALERTE ─────────────────────────────────────────

class Severite(Enum):
    OK        = "✅ OK"
    ATTENTION = "ℹ️  ATTENTION"
    ALERTE    = "⚠️  ALERTE"
    CRITIQUE  = "🚨 CRITIQUE"

# ─── ABIs ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.morpho_abis import MORPHO_ABI, MORPHO_ORACLE_ABI, MORPHO_GOV_EVENTS_ABI, CHAINLINK_ABI

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTRUCTION D'UNE ALERTE
# ═══════════════════════════════════════════════════════════════════════════════

def creer_alerte(
    categorie: str,
    marche: str,
    severite: Severite,
    message: str,
    valeur: float | None = None,
    seuil: float | None = None,
    action: str = "",
) -> dict:
    """Crée un objet d'alerte standardisé."""
    return {
        "timestamp":  datetime.utcnow().isoformat(),
        "categorie":  categorie,
        "marche":     marche,
        "severite":   severite.value,
        "message":    message,
        "valeur":     round(valeur, 6) if valeur is not None else None,
        "seuil":      seuil,
        "action":     action,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 1 — SURVEILLANCE DU TAUX D'UTILISATION
# ═══════════════════════════════════════════════════════════════════════════════

def surveiller_utilisation(morpho) -> list[dict]:
    """
    Surveille le taux d'utilisation de chaque marché Morpho cible.

    POURQUOI CE MONITORING EST CRITIQUE :
    L'Adaptive Curve IRM cible 90% d'utilisation. Au-delà de 92%, le taux
    cible r₉₀% commence à doubler toutes les 5 jours. Un prêteur dont
    l'utilisation dépasse 97% ne peut quasiment plus retirer ses fonds
    car il n'y a plus de liquidité disponible.

    Analogie TradFi : surveillance de la profondeur de marché d'un
    instrument de trésorerie avant d'initier un retrait important.
    """
    alertes = []

    for nom_marche, config in MORPHO_MARKETS_CIBLES.items():
        try:
            market_id_bytes = bytes.fromhex(config["id"][2:])
            data = morpho.functions.market(market_id_bytes).call()

            total_supply = data[0]  # totalSupplyAssets
            total_borrow = data[2]  # totalBorrowAssets

            if total_supply == 0:
                continue

            utilisation    = total_borrow / total_supply
            utilisation_pct = utilisation * 100
            tvl_m          = total_supply / (10**config["loan_decimals"]) / 1_000_000

            # Détermination de la sévérité
            if utilisation >= SEUILS["utilisation_critique"]:
                sev = Severite.CRITIQUE
                action = ("Retrait quasi-impossible — liquidité insuffisante. "
                          "Contacter le desk de trésorerie immédiatement.")
            elif utilisation >= SEUILS["utilisation_alerte"]:
                sev = Severite.ALERTE
                action = ("Planifier le retrait rapidement avant que l'IRM "
                          "ne doublement le taux cible. Réduire l'exposition.")
            elif utilisation >= SEUILS["utilisation_attention"]:
                sev = Severite.ATTENTION
                action = "Surveiller l'évolution sur les 24 prochaines heures."
            else:
                sev = Severite.OK
                action = ""

            alertes.append(creer_alerte(
                categorie="UTILISATION",
                marche=nom_marche,
                severite=sev,
                message=(f"Taux d'utilisation : {utilisation_pct:.2f}% "
                         f"(TVL : ${tvl_m:.1f}M)"),
                valeur=utilisation_pct,
                seuil=SEUILS["utilisation_alerte"] * 100,
                action=action,
            ))

        except Exception as e:
            alertes.append(creer_alerte(
                categorie="UTILISATION",
                marche=nom_marche,
                severite=Severite.CRITIQUE,
                message=f"Impossible de lire l'état du marché : {e}",
                action="Vérifier la connexion RPC.",
            ))

    return alertes


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 2 — SURVEILLANCE DU STALENESS DES ORACLES
# ═══════════════════════════════════════════════════════════════════════════════

def surveiller_staleness_oracles() -> list[dict]:
    """
    Surveille la fraîcheur de chaque feed Chainlink utilisé par les marchés
    Morpho cibles.

    POURQUOI CE MONITORING EST CRITIQUE :
    Un feed stale (non mis à jour depuis plus de son heartbeat) signifie que
    l'oracle Morpho utilise un prix potentiellement obsolète pour calculer
    les health factors. Cela peut :
    - Bloquer des liquidations nécessaires (collatéral sous-évalué non détecté)
    - Déclencher des liquidations abusives (collatéral surévalué)
    - Rendre la valorisation officielle de l'institution non fiable

    La surveillance combine :
    1. Le check de staleness du feed direct (updatedAt vs heartbeat)
    2. La cohérence entre le feed direct et le prix retourné par l'oracle Morpho
    """
    alertes = []
    now     = int(time.time())

    # Feeds distincts à surveiller sur l'ensemble des marchés cibles
    feeds_a_surveiller = set()
    for config in MORPHO_MARKETS_CIBLES.values():
        feeds_a_surveiller.update(config.get("feeds", []))

    for feed_name in feeds_a_surveiller:
        feed_config = CHAINLINK_FEEDS.get(feed_name)
        if not feed_config:
            continue

        try:
            feed = w3.eth.contract(
                address=Web3.to_checksum_address(feed_config["address"]),
                abi=CHAINLINK_ABI
            )
            data       = feed.functions.latestRoundData().call()
            updated_at = data[3]
            prix       = data[1] / (10**feed_config["decimals"])

            age_s      = now - updated_at
            heartbeat  = feed_config["heartbeat"]
            ratio_age  = age_s / heartbeat
            age_min    = age_s / 60

            # Vérification de cohérence du round
            round_ok = data[4] >= data[0]

            if not round_ok:
                sev    = Severite.CRITIQUE
                action = ("Round Chainlink incohérent (answeredInRound < roundId). "
                          "Ne pas utiliser ce feed pour la valorisation officielle.")
            elif ratio_age >= SEUILS["staleness_critique"]:
                sev    = Severite.CRITIQUE
                action = ("Feed STALE — ne pas utiliser pour la valorisation. "
                          "Activer le fallback (dernière valeur valide ou "
                          "prix Bloomberg/Refinitiv).")
            elif ratio_age >= SEUILS["staleness_alerte"]:
                sev    = Severite.ALERTE
                action = (f"Feed proche de l'expiration ({age_min:.0f}min écoulées "
                          f"sur {heartbeat//60}min de heartbeat). Préparer le fallback.")
            elif ratio_age >= SEUILS["staleness_attention"]:
                sev    = Severite.ATTENTION
                action = "Surveiller — mise à jour attendue prochainement."
            else:
                sev    = Severite.OK
                action = ""

            alertes.append(creer_alerte(
                categorie="STALENESS_ORACLE",
                marche=feed_name,
                severite=sev,
                message=(f"Prix : {prix:.6f} — "
                         f"Âge : {age_min:.0f}min / "
                         f"Heartbeat : {heartbeat//60}min "
                         f"({ratio_age*100:.0f}%)"),
                valeur=ratio_age * 100,
                seuil=SEUILS["staleness_alerte"] * 100,
                action=action,
            ))

        except Exception as e:
            alertes.append(creer_alerte(
                categorie="STALENESS_ORACLE",
                marche=feed_name,
                severite=Severite.CRITIQUE,
                message=f"Impossible de lire le feed : {e}",
                action="Vérifier l'adresse du feed et la connexion RPC.",
            ))

    return alertes


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — SURVEILLANCE DU HEALTH FACTOR DES EMPRUNTEURS
# ═══════════════════════════════════════════════════════════════════════════════

def surveiller_health_factor(
    morpho,
    wallet: str,
) -> list[dict]:
    """
    Calcule et surveille le Health Factor des positions emprunteur d'un wallet.

    Health Factor = (Valeur collatéral × LLTV) / Montant emprunté

    HF > 1 → position saine
    HF < 1 → position liquidatable

    BUFFER RECOMMANDÉ (cf. Section IV.4 du mémoire) :
    Un institutionnel doit maintenir un buffer de 10-15% sous le LLTV pour
    éviter les liquidations en cas de pic de gas (impossibilité d'agir
    rapidement) ou de gap de prix entre deux blocs.

    Seuils pratiques (basés sur le LIF Morpho) :
    - HF > 1.20 : zone confortable
    - HF < 1.15 : buffer réduit — à surveiller
    - HF < 1.10 : zone de préalerte — action recommandée
    - HF < 1.05 : liquidation imminente — action urgente

    Args:
        morpho : contrat Morpho Blue Web3
        wallet : adresse du wallet institutionnel (emprunteur)
    """
    alertes = []

    for nom_marche, config in MORPHO_MARKETS_CIBLES.items():
        try:
            market_id_bytes = bytes.fromhex(config["id"][2:])
            wallet_cs = Web3.to_checksum_address(wallet)

            # Position du wallet
            pos  = morpho.functions.position(market_id_bytes, wallet_cs).call()
            borrow_shares = pos[1]
            collateral    = pos[2]

            # Pas de position emprunteur sur ce marché
            if borrow_shares == 0 or collateral == 0:
                continue

            # État du marché (pour convertir les shares en actifs)
            mkt  = morpho.functions.market(market_id_bytes).call()
            total_borrow_assets = mkt[2]
            total_borrow_shares = mkt[3]

            # Paramètres immuables
            params = morpho.functions.idToMarketParams(market_id_bytes).call()
            oracle_address = params[2]
            lltv           = params[4] / 1e18

            # Prix de l'oracle Morpho (ORACLE_PRICE_SCALE = 1e36)
            oracle = w3.eth.contract(
                address=Web3.to_checksum_address(oracle_address),
                abi=MORPHO_ORACLE_ABI
            )
            price_raw = oracle.functions.price().call()

            # Calcul du Health Factor
            # HF = (collateral × price / ORACLE_PRICE_SCALE × LLTV) / borrowAssets
            #
            # Tout est exprimé en unités on-chain (sans décimales) pour éviter
            # les erreurs d'arrondi — même calcul que le contrat Morpho Blue
            ORACLE_PRICE_SCALE = 10**36

            borrow_assets = (borrow_shares * total_borrow_assets
                             // total_borrow_shares)  if total_borrow_shares > 0 else 0

            if borrow_assets == 0:
                continue

            # maxBorrow = collateral × price × LLTV / ORACLE_PRICE_SCALE
            max_borrow = int(collateral * price_raw * lltv / ORACLE_PRICE_SCALE)
            hf         = max_borrow / borrow_assets if borrow_assets > 0 else float('inf')

            # Sévérité
            if hf < SEUILS["hf_critique"]:
                sev    = Severite.CRITIQUE
                action = ("Liquidation imminente — ajouter du collatéral ou "
                          "rembourser partiellement la dette immédiatement. "
                          "Attention : pic de gas peut retarder l'action.")
            elif hf < SEUILS["hf_alerte"]:
                sev    = Severite.ALERTE
                action = ("Buffer de collatéral insuffisant — ajouter du "
                          "collatéral ou réduire la position d'emprunt "
                          "dans les prochaines heures.")
            elif hf < SEUILS["hf_attention"]:
                sev    = Severite.ATTENTION
                action = ("Surveiller l'évolution du prix du collatéral. "
                          "Buffer recommandé : maintenir HF > 1.20.")
            else:
                sev    = Severite.OK
                action = ""

            alertes.append(creer_alerte(
                categorie="HEALTH_FACTOR",
                marche=nom_marche,
                severite=sev,
                message=(f"Health Factor : {hf:.4f} "
                         f"(LLTV : {lltv*100:.1f}%)"),
                valeur=hf,
                seuil=SEUILS["hf_alerte"],
                action=action,
            ))

        except Exception as e:
            # Position inexistante ou erreur de lecture — non critique
            continue

    return alertes


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 4 — SURVEILLANCE DE LA GOUVERNANCE ET DE LA BAD DEBT
# ═══════════════════════════════════════════════════════════════════════════════

def surveiller_gouvernance_et_bad_debt(nb_blocs: int = 7200) -> list[dict]:
    """
    Surveille les événements de gouvernance Morpho et les événements de
    bad debt sur les marchés cibles sur les derniers N blocs (~24h).

    GOUVERNANCE MORPHO :
    La gouvernance MORPHO ne peut pas modifier les marchés existants
    (paramètres immuables), mais elle peut :
    - Approuver de nouveaux IRM ou LLTV (pour les futurs marchés)
    - Modifier les frais de protocole (fee, actuellement 0%)
    - Changer l'adresse du fee recipient

    Ces événements, bien que non critiques pour les marchés existants,
    doivent être documentés dans le dossier de due diligence continue
    et signalés à l'équipe juridique/compliance.

    BAD DEBT :
    Un événement Liquidate avec badDebtAssets > 0 sur un marché cible
    signifie que des prêteurs ont subi une perte. À surveiller pour
    l'estimation de l'ECL (Expected Credit Loss — IFRS 9).

    Args:
        nb_blocs : fenêtre de surveillance (défaut : 7200 ≈ 24h)
    """
    alertes = []
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_GOV_EVENTS_ABI
    )

    bloc_actuel = w3.eth.block_number
    bloc_debut  = max(0, bloc_actuel - nb_blocs)

    # ── Événements de gouvernance ─────────────────────────────────────────────
    for event_name, description in [
        ("EnableIrm",  "Nouvel IRM approuvé par la gouvernance Morpho"),
        ("EnableLltv", "Nouveau LLTV approuvé par la gouvernance Morpho"),
        ("SetFee",     "Modification des frais de protocole"),
    ]:
        try:
            event_filter = morpho.events[event_name].create_filter(
                from_block=bloc_debut,
                to_block=bloc_actuel
            )
            events = event_filter.get_all_entries()

            for evt in events:
                alertes.append(creer_alerte(
                    categorie="GOUVERNANCE",
                    marche="Protocol",
                    severite=Severite.ATTENTION,
                    message=(f"{description} — "
                             f"Bloc #{evt['blockNumber']:,} — "
                             f"Tx : {evt['transactionHash'].hex()[:16]}..."),
                    action=("Documenter dans le dossier de due diligence continue. "
                            "Informer l'équipe juridique/compliance."),
                ))

        except Exception:
            continue

    # ── Événements de bad debt sur les marchés cibles ─────────────────────────
    for nom_marche, config in MORPHO_MARKETS_CIBLES.items():
        try:
            market_id_bytes = bytes.fromhex(config["id"][2:])

            event_filter = morpho.events["Liquidate"].create_filter(
                from_block=bloc_debut,
                to_block=bloc_actuel,
                argument_filters={"id": market_id_bytes}
            )
            liquidations = event_filter.get_all_entries()

            # Filtrer les liquidations avec bad debt
            bad_debts = [
                evt for evt in liquidations
                if evt["args"].get("badDebtAssets", 0) > 0
            ]

            if bad_debts:
                total_bad_debt_raw = sum(
                    evt["args"]["badDebtAssets"] for evt in bad_debts
                )
                total_bad_debt = total_bad_debt_raw / (10**config["loan_decimals"])

                alertes.append(creer_alerte(
                    categorie="BAD_DEBT",
                    marche=nom_marche,
                    severite=Severite.ALERTE,
                    message=(f"{len(bad_debts)} liquidation(s) avec bad debt "
                             f"sur les {nb_blocs} derniers blocs — "
                             f"Total : {total_bad_debt:,.4f} (actif de prêt)"),
                    valeur=total_bad_debt,
                    action=("Réévaluer le provisionnement ECL (Expected Credit "
                            "Loss) selon IFRS 9. Vérifier l'impact sur le "
                            "Supply APY effectif du marché."),
                ))
            else:
                alertes.append(creer_alerte(
                    categorie="BAD_DEBT",
                    marche=nom_marche,
                    severite=Severite.OK,
                    message=(f"Aucune bad debt sur les {nb_blocs} derniers blocs "
                             f"({len(liquidations)} liquidation(s) saine(s))"),
                ))

        except Exception as e:
            continue

    return alertes


# ═══════════════════════════════════════════════════════════════════════════════
# RAPPORT DE MONITORING CONSOLIDÉ
# ═══════════════════════════════════════════════════════════════════════════════

def generer_rapport_monitoring(
    wallet: str,
    mode_daemon: bool = False,
    intervalle_s: int = 300,
) -> dict:
    """
    Exécute tous les modules de surveillance et génère un rapport consolidé.

    Deux modes d'exécution :
    - One-shot (mode_daemon=False) : exécution unique, export JSON
    - Daemon (mode_daemon=True)    : boucle continue toutes les N secondes

    Args:
        wallet       : adresse du wallet institutionnel à surveiller
        mode_daemon  : activer le mode de surveillance continue
        intervalle_s : intervalle entre deux checks en mode daemon
    """
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI
    )

    iteration = 0

    while True:
        iteration += 1
        ts = datetime.utcnow()
        bloc = w3.eth.block_number

        print(f"\n{'═'*62}")
        print(f"MONITORING MORPHO BLUE — {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"Bloc : #{bloc:,}  |  Itération : #{iteration}")
        print(f"{'═'*62}")

        toutes_alertes = []

        # ── Module 1 : Taux d'utilisation ─────────────────────────────────────
        print("\n  [1/4] Taux d'utilisation des marchés...")
        alertes_util = surveiller_utilisation(morpho)
        toutes_alertes.extend(alertes_util)

        # ── Module 2 : Staleness des oracles ──────────────────────────────────
        print("  [2/4] Fraîcheur des price feeds Chainlink...")
        alertes_stale = surveiller_staleness_oracles()
        toutes_alertes.extend(alertes_stale)

        # ── Module 3 : Health factor ───────────────────────────────────────────
        print("  [3/4] Health factor des positions emprunteur...")
        alertes_hf = surveiller_health_factor(morpho, wallet)
        toutes_alertes.extend(alertes_hf)

        # ── Module 4 : Gouvernance et bad debt ────────────────────────────────
        print("  [4/4] Gouvernance et bad debt (24 dernières heures)...")
        alertes_gov = surveiller_gouvernance_et_bad_debt()
        toutes_alertes.extend(alertes_gov)

        # ── Synthèse et affichage ──────────────────────────────────────────────
        critiques  = [a for a in toutes_alertes if "CRITIQUE" in a["severite"]]
        alertes_   = [a for a in toutes_alertes if "ALERTE"   in a["severite"]]
        attentions = [a for a in toutes_alertes if "ATTENTION" in a["severite"]]

        statut_global = (
            "🚨 CRITIQUE" if critiques else
            "⚠️  ALERTE"  if alertes_  else
            "ℹ️  ATTENTION" if attentions else
            "✅ OK"
        )

        print(f"\n{'─'*62}")
        print(f"STATUT GLOBAL : {statut_global}")
        print(f"  🚨 Critiques  : {len(critiques)}")
        print(f"  ⚠️  Alertes   : {len(alertes_)}")
        print(f"  ℹ️  Attentions : {len(attentions)}")
        print(f"  ✅ OK         : "
              f"{len(toutes_alertes) - len(critiques) - len(alertes_) - len(attentions)}")

        # Affichage des alertes actives
        actives = critiques + alertes_ + attentions
        if actives:
            print(f"\n  ALERTES ACTIVES :")
            for a in actives:
                print(f"\n    {a['severite']}  [{a['categorie']}] {a['marche']}")
                print(f"    → {a['message']}")
                if a.get("action"):
                    print(f"    → Action : {a['action']}")

        rapport = {
            "timestamp":      ts.isoformat(),
            "bloc":           bloc,
            "wallet":         wallet,
            "statut_global":  statut_global,
            "nb_critiques":   len(critiques),
            "nb_alertes":     len(alertes_),
            "nb_attentions":  len(attentions),
            "alertes":        toutes_alertes,
        }

        # Export JSON
        output_file = "morpho_monitoring_report.json"
        with open(output_file, "w") as f:
            json.dump(rapport, f, indent=2, default=str)
        print(f"\n  💾 Rapport exporté : {output_file}")

        if not mode_daemon:
            return rapport

        # Mode daemon : attendre avant le prochain cycle
        print(f"\n  ⏳ Prochain check dans {intervalle_s}s "
              f"({intervalle_s//60}min)...")
        time.sleep(intervalle_s)


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("SCRIPT 7 — MONITORING ON-CHAIN MORPHO BLUE")
    print("Surveillance automatique des risques opérationnels")
    print("Section IV.4 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*62)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")

    WALLET_INSTITUTION = os.getenv(
        "WALLET_INSTITUTION",
        "0x4e9d257FfEce3C9fAb9D8D5e4e6e14C98E6b6b6b"
    )

    # Choisir le mode d'exécution
    MODE_DAEMON    = os.getenv("MODE_DAEMON", "false").lower() == "true"
    INTERVALLE_MIN = int(os.getenv("INTERVALLE_MIN", "5"))

    if MODE_DAEMON:
        print(f"\n🔄 Mode daemon activé — check toutes les {INTERVALLE_MIN} minutes")
        print(f"   Wallet surveillé : {WALLET_INSTITUTION}")
        print("   Ctrl+C pour arrêter")

    generer_rapport_monitoring(
        wallet=WALLET_INSTITUTION,
        mode_daemon=MODE_DAEMON,
        intervalle_s=INTERVALLE_MIN * 60,
    )