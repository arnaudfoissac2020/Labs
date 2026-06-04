"""
SCRIPT 1 — Analyse on-chain d'un protocole DeFi : TVL, concentration des liquidités
             et participation à la gouvernance (Aave V3 sur Ethereum)

Contexte : Ce script illustre comment un analyste institutionnel peut interroger
           directement la blockchain Ethereum pour évaluer la solidité opérationnelle
           d'un protocole DeFi avant toute exposition. Les trois modules couvrent
           les métriques fondamentales d'une due diligence on-chain.

Dépendances :
    pip install requests pandas web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>   # Nœud Ethereum (Infura, Alchemy...)

Sources :
    - The Graph : subgraph Aave V3 Ethereum
      https://thegraph.com/explorer/subgraphs/Cd2gEDVeqnjBn1hSeqFMitw8Q1iiyV9FYUZkLNRcL32
    - Aave V3 documentation : https://docs.aave.com/developers/
    - Aave Governance : https://governance.aave.com/
"""

import os
import requests
import json
from datetime import datetime, timedelta
import pandas as pd
from web3 import Web3
#from dotenv import load_dotenv

#load_dotenv()

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

# Endpoint The Graph — subgraph Aave V3 Ethereum (décentralisé, pas de clé requise)
api_key = 'ad7be9979bdef2600a0e09a4ddc01d69' # My AFO generated Graph Key

AAVE_SUBGRAPH_URL = (
    f'https://gateway.thegraph.com/api/{api_key}/subgraphs/id/JCNWRypm7FYwV8fx5HhzZPSFaMxgkPuw4TnR3Gpi81zk'
)

sm_contract = "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e" #Aave
#sm_contract = "0x7712c34205737192402172409a8F7ccef8aA2AEc" #BUIDL


#url = f'https://gateway.thegraph.com/api/{api_key}/subgraphs/id/JCNWRypm7FYwV8fx5HhzZPSFaMxgkPuw4TnR3Gpi81zk'

# Endpoint The Graph — subgraph Aave Governance
AAVE_GOV_SUBGRAPH_URL = (
    "https://gateway.thegraph.com/api/subgraphs/id/"
    "HtcnAQ7KLGGp1HrtE6FUbNFSCuP6BJzMHPyH5D3MKo1C"
)

# Connexion RPC Ethereum (lecture seule — aucune clé privée impliquée)
#RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")  # RPC public en fallback
RPC_URL = os.getenv("RPC_URL", "https://eth-mainnet.g.alchemy.com/v2/dG94gIWl7BXxTvY0Wrk8f") 


w3 = Web3(Web3.HTTPProvider(RPC_URL))


def run_graphql_query(url: str, query: str, variables: dict = None) -> dict:
    """
    Exécute une requête GraphQL sur un subgraph The Graph.

    The Graph est un protocole d'indexation décentralisé qui permet d'interroger
    les données on-chain via des requêtes GraphQL structurées, sans avoir à
    parcourir directement les blocs de la blockchain.

    Args:
        url       : URL du subgraph The Graph
        query     : Requête GraphQL
        variables : Variables de la requête (optionnel)

    Returns:
        Dictionnaire JSON de la réponse
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


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 1 — TVL HISTORIQUE (Total Value Locked)
# ═══════════════════════════════════════════════════════════════════════════════

def get_aave_tvl_history(days: int = 30) -> pd.DataFrame:
    """
    Récupère l'historique du TVL d'Aave V3 sur Ethereum sur les N derniers jours.

    Le TVL (Total Value Locked) mesure la valeur totale des actifs déposés dans
    le protocole. C'est la métrique de référence pour évaluer la taille et la
    maturité d'un protocole DeFi. Cependant, un TVL élevé n'est pas suffisant :
    il faut analyser sa stabilité et sa composition (voir Module 2).

    Signaux d'alerte :
        - Baisse soudaine > 20% du TVL en 24h : retrait massif de liquidités
        - Concentration > 50% du TVL sur un seul actif : risque de concentration
        - TVL artificiellement gonflé par des boucles de dépôt/emprunt

    Args:
        days : Nombre de jours d'historique à récupérer

    Returns:
        DataFrame avec colonnes [date, tvl_usd, tvl_eth]
    """
    # Calcul du timestamp de début (Unix timestamp)
    start_timestamp = int((datetime.utcnow() - timedelta(days=days)).timestamp())

    # Requête GraphQL : snapshots journaliers du protocole
    # financialsDailySnapshots = instantanés quotidiens des métriques financières
    query = """
    query GetTVLHistory($startTime: Int!) {
      financialsDailySnapshots(
        first: 1000
        where: { timestamp_gte: $startTime }
        orderBy: timestamp
        orderDirection: asc
        protocol: "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e"
      ) {
        timestamp
        totalValueLockedUSD
        totalDepositBalanceUSD
        totalBorrowBalanceUSD
        dailySupplySideRevenueUSD
        dailyProtocolSideRevenueUSD
      }
    }
    """

    data = run_graphql_query(
        AAVE_SUBGRAPH_URL,
        query,
        variables={"startTime": start_timestamp}
    )

    snapshots = data.get("financialsDailySnapshots", [])
    print(snapshots)
    if not snapshots:
        print("⚠️  Aucune donnée TVL disponible — vérifier l'endpoint du subgraph.")
        return pd.DataFrame()

    rows = []
    for snap in snapshots:
        rows.append({
            "date": datetime.utcfromtimestamp(int(snap["timestamp"])).strftime("%Y-%m-%d"),
            "tvl_usd": float(snap["totalValueLockedUSD"]),
            "total_deposits_usd": float(snap["totalDepositBalanceUSD"]),
            "total_borrows_usd": float(snap["totalBorrowBalanceUSD"]),
            # Utilization rate : ratio emprunts/dépôts — indicateur de santé du protocole
            # Un taux > 90% peut indiquer un risque de liquidité (les déposants ne peuvent
            # plus retirer librement)
            "utilization_rate": (
                float(snap["totalBorrowBalanceUSD"]) /
                float(snap["totalDepositBalanceUSD"])
                if float(snap["totalDepositBalanceUSD"]) > 0 else 0
            ),
            "daily_revenue_usd": (
                float(snap["dailySupplySideRevenueUSD"]) +
                float(snap["dailyProtocolSideRevenueUSD"])
            ),
        })

    df = pd.DataFrame(rows)

    # Calcul de la variation journalière du TVL
    df["tvl_change_pct"] = df["tvl_usd"].pct_change() * 100

    return df


def analyze_tvl(df: pd.DataFrame) -> None:
    """
    Analyse et affiche les signaux d'alerte du TVL pour la due diligence.
    """
    if df.empty:
        return

    print("\n" + "═" * 60)
    print("MODULE 1 — ANALYSE DU TVL (Total Value Locked)")
    print("═" * 60)

    latest = df.iloc[-1]
    oldest = df.iloc[0]

    print(f"\n📊 TVL actuel          : ${latest['tvl_usd']:,.0f}")
    print(f"   Dépôts totaux      : ${latest['total_deposits_usd']:,.0f}")
    print(f"   Emprunts totaux    : ${latest['total_borrows_usd']:,.0f}")
    print(f"   Taux d'utilisation : {latest['utilization_rate']:.1%}")
    print(f"   Revenu journalier  : ${latest['daily_revenue_usd']:,.0f}")

    tvl_change_period = (latest["tvl_usd"] / oldest["tvl_usd"] - 1) * 100
    print(f"\n📈 Variation TVL sur {len(df)}j : {tvl_change_period:+.1f}%")

    # Signaux d'alerte
    print("\n🔍 Signaux d'alerte :")
    max_daily_drop = df["tvl_change_pct"].min()
    if max_daily_drop < -20:
        print(f"   ⚠️  ALERTE : Baisse journalière maximale de {max_daily_drop:.1f}%")
    else:
        print(f"   ✅ Variation journalière max : {max_daily_drop:.1f}% (dans les limites)")

    if latest["utilization_rate"] > 0.90:
        print(f"   ⚠️  ALERTE : Taux d'utilisation élevé ({latest['utilization_rate']:.1%})")
        print("       Risque de liquidité : les déposants pourraient ne pas pouvoir retirer")
    else:
        print(f"   ✅ Taux d'utilisation : {latest['utilization_rate']:.1%} (nominal)")

    print(f"\n📋 Top 5 variations journalières :")
    top_moves = df.nlargest(5, "tvl_change_pct")[["date", "tvl_usd", "tvl_change_pct"]]
    for _, row in top_moves.iterrows():
        print(f"   {row['date']} : ${row['tvl_usd']:,.0f} ({row['tvl_change_pct']:+.1f}%)")


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 2 — CONCENTRATION DES LIQUIDITÉS PAR ACTIF
# ═══════════════════════════════════════════════════════════════════════════════

def get_aave_market_composition() -> pd.DataFrame:
    """
    Récupère la composition des marchés Aave V3 par actif déposé.

    La concentration des liquidités est un indicateur de risque critique pour
    un institutionnel. Un protocole dont 80% du TVL repose sur un seul actif
    est exposé à un risque systémique en cas de dépeg ou de forte volatilité
    de cet actif.

    Métriques clés :
        - Herfindahl-Hirschman Index (HHI) : mesure de concentration de marché
          HHI < 1500 : marché peu concentré
          HHI 1500-2500 : marché modérément concentré
          HHI > 2500 : marché très concentré (signal d'alerte pour la due diligence)

    Returns:
        DataFrame avec la composition des marchés et métriques de concentration
    """
    query = """
    {
      markets(
        first: 50
        orderBy: totalValueLockedUSD
        orderDirection: desc
        where: {
          protocol: "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e"
          isActive: true
        }
      ) {
        name
        inputToken {
          symbol
          decimals
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
      }
    }
    """
    query = '''{
    markets(first: 1000) {
    id
    name
    indexLastUpdatedTimestamp

    ##### Tokens #####
    inputToken { id symbol decimals }
    inputTokenBalance
    inputTokenPriceUSD

    outputToken { id symbol decimals }
    outputTokenSupply
    outputTokenPriceUSD

    ##### Pool Policy #####
    canBorrowFrom
    canUseAsCollateral
    maximumLTV
    liquidationThreshold
    liquidationPenalty
    reserveFactor
    exchangeRate

    ##### Balance Sheet #####
    totalValueLockedUSD
    totalDepositBalanceUSD
    totalBorrowBalanceUSD
    cumulativeSupplySideRevenueUSD
    cumulativeProtocolSideRevenueUSD
    cumulativeTotalRevenueUSD
     }
    }'''


    data = run_graphql_query(AAVE_SUBGRAPH_URL, query)
    markets = data.get("markets", [])
    print ("coucou Markets")
    print(markets)
    if not markets:
        print("⚠️  Aucun marché disponible.")
        return pd.DataFrame()

    rows = []
    total_tvl = sum(float(m["totalValueLockedUSD"]) for m in markets)

    for market in markets:
        tvl = float(market["totalValueLockedUSD"])
        deposits = float(market["totalDepositBalanceUSD"])
        borrows = float(market["totalBorrowBalanceUSD"])

        # Extraction du taux de dépôt (supply rate)
        supply_rate = 0
        for rate in market.get("rates", []):
            if rate.get("side") == "LENDER" and rate.get("type") == "VARIABLE":
                supply_rate = float(rate.get("rate", 0))
                break

        rows.append({
            "asset": market["inputToken"]["symbol"],
            "tvl_usd": tvl,
            "tvl_share_pct": (tvl / total_tvl * 100) if total_tvl > 0 else 0,
            "deposits_usd": deposits,
            "borrows_usd": borrows,
            "utilization_rate": (borrows / deposits) if deposits > 0 else 0,
            "supply_apy_pct": supply_rate * 100,
            "liquidation_threshold": float(market.get("liquidationThreshold", 0)),
            "borrowable": market.get("canBorrowFrom", False),
        })

    df = pd.DataFrame(rows)
    df = df.sort_values("tvl_usd", ascending=False).reset_index(drop=True)

    return df, total_tvl


def analyze_concentration(df: pd.DataFrame, total_tvl: float) -> None:
    """
    Analyse la concentration des liquidités et calcule le HHI.
    """
    if df.empty:
        return

    print("\n" + "═" * 60)
    print("MODULE 2 — CONCENTRATION DES LIQUIDITÉS PAR ACTIF")
    print("═" * 60)

    # Calcul du HHI (Herfindahl-Hirschman Index)
    # HHI = somme des carrés des parts de marché (en %)
    hhi = sum((row["tvl_share_pct"] ** 2) for _, row in df.iterrows())

    print(f"\n📊 TVL total protocole : ${total_tvl:,.0f}")
    print(f"   Nombre de marchés  : {len(df)}")
    print(f"   HHI (concentration): {hhi:.0f}", end="  ")

    if hhi < 1500:
        print("✅ Marché peu concentré")
    elif hhi < 2500:
        print("⚠️  Marché modérément concentré")
    else:
        print("🚨 Marché très concentré — signal d'alerte")

    print(f"\n📋 Composition du TVL (top 10 actifs) :")
    print(f"{'Actif':<12} {'TVL ($M)':>10} {'Part %':>8} {'Utilisation':>12} {'APY Supply':>11}")
    print("-" * 57)

    for _, row in df.head(10).iterrows():
        print(
            f"{row['asset']:<12} "
            f"${row['tvl_usd']/1e6:>8.1f}M "
            f"{row['tvl_share_pct']:>7.1f}% "
            f"{row['utilization_rate']:>11.1%} "
            f"{row['supply_apy_pct']:>10.2f}%"
        )

    # Part cumulée du top 3
    top3_share = df.head(3)["tvl_share_pct"].sum()
    print(f"\n   Part des 3 premiers actifs : {top3_share:.1f}%", end="  ")
    if top3_share > 70:
        print("⚠️  Concentration élevée")
    else:
        print("✅ Diversification acceptable")


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — PARTICIPATION À LA GOUVERNANCE
# ═══════════════════════════════════════════════════════════════════════════════

def get_aave_governance_activity(last_n_proposals: int = 10) -> pd.DataFrame:
    """
    Analyse la participation à la gouvernance Aave sur les dernières propositions.

    La gouvernance d'un protocole DeFi est un vecteur de risque souvent sous-estimé
    par les institutions financières. Une faible participation signifie qu'une
    minorité de détenteurs de tokens peut modifier les paramètres du protocole
    (taux, collatéraux, plafonds d'exposition), ce qui constitue un risque
    opérationnel pour toute position institutionnelle.

    Signaux d'alerte :
        - Quorum participation < 10% de l'offre circulante : gouvernance vulnérable
        - Concentration des votes (1 adresse > 50% des votes) : risque de contrôle
        - Propositions modifiant les paramètres de risque : surveiller activement

    Args:
        last_n_proposals : Nombre de dernières propositions à analyser

    Returns:
        DataFrame avec métriques de participation par proposition
    """
    # Note : Aave utilise son propre système de gouvernance on-chain
    # Les propositions sont votées par les détenteurs de stkAAVE et AAVE
    query = """
    query GetGovernanceActivity($first: Int!) {
      proposals(
        first: $first
        orderBy: creationTime
        orderDirection: desc
      ) {
        id
        title
        state
        creationTime
        startBlock
        endBlock
        currentYesVote
        currentNoVote
        totalCurrentVoters
        executor {
          id
        }
      }
    }
    """

    data = run_graphql_query(
        AAVE_GOV_SUBGRAPH_URL,
        query,
        variables={"first": last_n_proposals}
    )

    proposals = data.get("proposals", [])

    if not proposals:
        print("⚠️  Aucune proposition de gouvernance disponible.")
        return pd.DataFrame()

    rows = []
    for prop in proposals:
        yes_votes = float(prop.get("currentYesVote", 0)) / 1e18  # Conversion wei → AAVE
        no_votes = float(prop.get("currentNoVote", 0)) / 1e18
        total_votes = yes_votes + no_votes
        total_voters = int(prop.get("totalCurrentVoters", 0))

        rows.append({
            "proposal_id": prop["id"],
            "title": prop.get("title", "N/A")[:60],  # Tronqué pour l'affichage
            "state": prop.get("state", "N/A"),
            "date": datetime.utcfromtimestamp(
                int(prop["creationTime"])
            ).strftime("%Y-%m-%d") if prop.get("creationTime") else "N/A",
            "yes_votes_aave": yes_votes,
            "no_votes_aave": no_votes,
            "total_votes_aave": total_votes,
            "total_voters": total_voters,
            # Taux d'approbation : part des votes favorables
            "approval_rate": (yes_votes / total_votes) if total_votes > 0 else 0,
            # Rapport votes/votants : indique si quelques gros détenteurs dominent
            "avg_votes_per_voter": (total_votes / total_voters) if total_voters > 0 else 0,
        })

    return pd.DataFrame(rows)


def analyze_governance(df: pd.DataFrame) -> None:
    """
    Analyse la participation à la gouvernance et génère un scoring de risque.
    """
    if df.empty:
        return

    print("\n" + "═" * 60)
    print("MODULE 3 — PARTICIPATION À LA GOUVERNANCE")
    print("═" * 60)

    executed = df[df["state"] == "EXECUTED"]
    avg_voters = df["total_voters"].mean()
    avg_votes = df["total_votes_aave"].mean()
    avg_approval = df["approval_rate"].mean()

    print(f"\n📊 Propositions analysées  : {len(df)}")
    print(f"   Propositions exécutées  : {len(executed)}")
    print(f"   Votants moyens/prop     : {avg_voters:.0f} adresses")
    print(f"   Votes moyens/prop       : {avg_votes:,.0f} AAVE")
    print(f"   Taux d'approbation moy  : {avg_approval:.1%}")

    print(f"\n📋 Dernières propositions :")
    print(f"{'ID':<6} {'Date':<12} {'État':<12} {'Votes (AAVE)':>14} {'Approbation':>12} {'Votants':>8}")
    print("-" * 68)

    for _, row in df.head(10).iterrows():
        state_icon = {"EXECUTED": "✅", "FAILED": "❌", "ACTIVE": "🔵", "CANCELED": "⛔"}.get(
            row["state"], "⏳"
        )
        print(
            f"{str(row['proposal_id']):<6} "
            f"{row['date']:<12} "
            f"{state_icon} {row['state']:<10} "
            f"{row['total_votes_aave']:>13,.0f} "
            f"{row['approval_rate']:>11.1%} "
            f"{row['total_voters']:>7}"
        )

    # Score de risque gouvernance (simplifié)
    print(f"\n🔍 Évaluation du risque de gouvernance :")
    risk_score = 0

    if avg_voters < 100:
        print(f"   ⚠️  Faible nombre de votants ({avg_voters:.0f}) — gouvernance concentrée")
        risk_score += 2
    else:
        print(f"   ✅ Participation suffisante ({avg_voters:.0f} votants en moyenne)")

    # Vérification de la concentration des votes
    if df["avg_votes_per_voter"].mean() > 100000:
        print(f"   ⚠️  Votes très concentrés ({df['avg_votes_per_voter'].mean():,.0f} AAVE/votant)")
        print(f"       Quelques whales peuvent contrôler les décisions")
        risk_score += 1
    else:
        print(f"   ✅ Distribution des votes acceptable")

    risk_labels = {0: "FAIBLE", 1: "MODÉRÉ", 2: "ÉLEVÉ", 3: "CRITIQUE"}
    print(f"\n   Score de risque gouvernance : {risk_labels.get(risk_score, 'ÉLEVÉ')}")


# ═══════════════════════════════════════════════════════════════════════════════
# MODULE 4 — SYNTHÈSE : SCORING DE DUE DILIGENCE ON-CHAIN
# ═══════════════════════════════════════════════════════════════════════════════

def generate_due_diligence_report(
    tvl_df: pd.DataFrame,
    market_df: pd.DataFrame,
    total_tvl: float,
    gov_df: pd.DataFrame,
    protocol_name: str = "Aave V3"
) -> dict:
    """
    Génère un rapport de scoring synthétique pour la due diligence on-chain.

    Ce scoring est conçu pour être intégré dans une grille d'évaluation plus large
    (incluant l'audit de smart contracts et les critères off-chain) telle que
    présentée dans la Section II.3 du mémoire.

    Dimensions évaluées :
        1. Solidité du TVL      : taille, stabilité, trend
        2. Concentration        : diversification des actifs
        3. Santé du protocole   : utilisation, revenus
        4. Gouvernance          : participation, décentralisation

    Returns:
        Dictionnaire contenant les scores par dimension et le score global
    """
    scores = {}
    details = {}

    # ── Dimension 1 : Solidité du TVL ─────────────────────────────────
    if not tvl_df.empty:
        max_daily_drop = tvl_df["tvl_change_pct"].min()
        tvl_trend = (
            tvl_df.iloc[-1]["tvl_usd"] / tvl_df.iloc[0]["tvl_usd"] - 1
        ) * 100 if len(tvl_df) > 1 else 0

        tvl_score = 4  # Score de base
        if max_daily_drop < -30:
            tvl_score -= 2
        elif max_daily_drop < -15:
            tvl_score -= 1
        if tvl_trend < -20:
            tvl_score -= 1
        if total_tvl < 100_000_000:  # < 100M$ : protocole trop petit
            tvl_score -= 2

        scores["tvl_solidity"] = max(1, min(4, tvl_score))
        details["tvl"] = {
            "total_usd": total_tvl,
            "max_daily_drop_pct": max_daily_drop,
            "trend_pct": tvl_trend,
        }

    # ── Dimension 2 : Concentration des liquidités ────────────────────
    if not market_df.empty:
        hhi = sum((row["tvl_share_pct"] ** 2) for _, row in market_df.iterrows())
        top3_share = market_df.head(3)["tvl_share_pct"].sum()

        if hhi < 1500:
            conc_score = 4
        elif hhi < 2000:
            conc_score = 3
        elif hhi < 2500:
            conc_score = 2
        else:
            conc_score = 1

        scores["concentration"] = conc_score
        details["concentration"] = {"hhi": hhi, "top3_share_pct": top3_share}

    # ── Dimension 3 : Santé du protocole ──────────────────────────────
    if not market_df.empty:
        avg_utilization = market_df["utilization_rate"].mean()
        health_score = 4
        if avg_utilization > 0.90:
            health_score -= 2  # Risque de liquidité critique
        elif avg_utilization > 0.80:
            health_score -= 1
        if avg_utilization < 0.10:
            health_score -= 1  # Protocole sous-utilisé : modèle économique fragile

        scores["protocol_health"] = max(1, min(4, health_score))
        details["health"] = {"avg_utilization": avg_utilization}

    # ── Dimension 4 : Gouvernance ─────────────────────────────────────
    if not gov_df.empty:
        avg_voters = gov_df["total_voters"].mean()
        gov_score = 4
        if avg_voters < 50:
            gov_score -= 3
        elif avg_voters < 100:
            gov_score -= 2
        elif avg_voters < 200:
            gov_score -= 1

        scores["governance"] = max(1, min(4, gov_score))
        details["governance"] = {"avg_voters_per_proposal": avg_voters}

    # ── Score global ──────────────────────────────────────────────────
    if scores:
        global_score = sum(scores.values()) / len(scores)
        scores["global"] = round(global_score, 2)

    # Affichage du rapport
    print("\n" + "═" * 60)
    print(f"RAPPORT DE DUE DILIGENCE ON-CHAIN — {protocol_name.upper()}")
    print(f"Date d'analyse : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("═" * 60)

    labels = {
        "tvl_solidity": "Solidité du TVL",
        "concentration": "Diversification des actifs",
        "protocol_health": "Santé opérationnelle",
        "governance": "Qualité de la gouvernance",
    }

    rating_labels = {4: "FORT", 3: "SATISFAISANT", 2: "MODÉRÉ", 1: "FAIBLE"}

    for key, label in labels.items():
        if key in scores:
            score = scores[key]
            bar = "█" * score + "░" * (4 - score)
            print(f"\n   {label:<30} [{bar}] {score}/4 — {rating_labels.get(score, '')}")

    global_score = scores.get("global", 0)
    global_label = (
        "ÉLIGIBLE À L'EXPOSITION" if global_score >= 3.0
        else "EXPOSITION CONDITIONNELLE" if global_score >= 2.0
        else "NON ÉLIGIBLE — DUE DILIGENCE INSUFFISANTE"
    )

    print(f"\n{'─'*60}")
    print(f"   SCORE GLOBAL : {global_score:.2f}/4.00")
    print(f"   VERDICT      : {global_label}")
    print(f"{'─'*60}")

    return {"scores": scores, "details": details, "verdict": global_label}


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":

    print("━" * 60)
    print("ANALYSE ON-CHAIN — AAVE V3 ETHEREUM")
    print("Due diligence institutionnelle — Section II.1")
    print("━" * 60)

    # Vérification de la connexion RPC
    if w3.is_connected():
        latest_block = w3.eth.block_number
        print(f"\n✅ Connexion RPC établie — Bloc actuel : #{latest_block:,}")
    else:
        print("\n⚠️  Connexion RPC indisponible — les modules GraphQL restent opérationnels")

    # ── Module 1 : TVL historique ──────────────────────────────────────
    print("\n⏳ Récupération du TVL historique (30 jours)...")
    tvl_df = get_aave_tvl_history(days=1)
    if not tvl_df.empty:
        analyze_tvl(tvl_df)

    # ── Module 2 : Composition des marchés ────────────────────────────
    print("\n⏳ Récupération de la composition des marchés...")
    result = get_aave_market_composition()
    if result:
        market_df, total_tvl = result
        analyze_concentration(market_df, total_tvl)
    else:
        market_df = pd.DataFrame()
        total_tvl = 0

    # ── Module 3 : Gouvernance ─────────────────────────────────────────
    print("\n⏳ Récupération des données de gouvernance...")
    gov_df = get_aave_governance_activity(last_n_proposals=10)
    if not gov_df.empty:
        analyze_governance(gov_df)

    # ── Module 4 : Rapport de synthèse ────────────────────────────────
    report = generate_due_diligence_report(
        tvl_df=tvl_df,
        market_df=market_df,
        total_tvl=total_tvl,
        gov_df=gov_df,
        protocol_name="Aave V3 Ethereum"
    )

    # Export JSON pour intégration dans les systèmes internes
    output_file = "aave_due_diligence_report.json"
    with open(output_file, "w") as f:
        # Conversion des valeurs numpy pour la sérialisation JSON
        serializable_report = {
            "protocol": "Aave V3 Ethereum",
            "analysis_date": datetime.utcnow().isoformat(),
            "scores": report["scores"],
            "verdict": report["verdict"],
        }
        json.dump(serializable_report, f, indent=2)

    print(f"\n💾 Rapport exporté : {output_file}")
    print("\n━" * 60)
