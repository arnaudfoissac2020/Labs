"""
SCRIPT 1 — Due diligence multi-protocoles : screening DeFi Llama et analyse
             approfondie des protocoles de lending sur Ethereum (Aave V3 + Morpho)

Contexte : Ce script illustre la démarche de due diligence on-chain en deux temps
           qu'un analyste institutionnel appliquerait avant toute exposition à un
           protocole DeFi de lending sur Ethereum :

           NIVEAU 1 — Screening rapide via DeFi Llama API (5 protocoles)
                      Identification des protocoles éligibles sur critères fondamentaux

           NIVEAU 2 — Analyse approfondie via The Graph (Aave V3 + Morpho)
                      Due diligence détaillée sur les protocoles retenus en Niveau 1

           Nota bene : Morpho est particulièrement pertinent dans ce contexte
           institutionnel — SG Forge l'utilise comme infrastructure de lending
           pour ses opérations DeFi réglementées (cf. Section 1.3 du mémoire).

Dépendances :
    pip install requests pandas web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
    - DeFi Llama API      : https://defillama.com/docs/api
    - The Graph Aave V3   : https://thegraph.com/explorer/subgraphs/Cd2gEDVeqnjBn1hSeqFMitw8Q1iiyV9FYUZkLNRcL32
    - The Graph Morpho    : https://thegraph.com/explorer/subgraphs/BoKHAYHEjrXzCtDVKsxGBiAbMbY3pnoaGqeFSKF6yqPM
"""

import os
import requests
import json
from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv
from query_helper import run_graphql_query
from query_helper import fetch_defillama

load_dotenv()

# ─── CONFIGURATION ────────────────────────────────────────────────────────────



# Identifiants des protocoles dans DeFi Llama
# Source : https://defillama.com/protocols/lending (colonne "slug")
PROTOCOLS_SCREENING = {
    "Aave V3":      "aave-v3",
    "Compound III": "compound-v3",
    "Spark":        "spark",
    "Morpho":       "morpho",
    "Euler V2":     "euler-v2",
}

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

# Adresses des protocoles sur Ethereum (pour référence et vérification on-chain)
PROTOCOL_ADDRESSES = {
    "Aave V3":  "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e",  # Pool Addresses Provider
    "Morpho":   "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb",  # Morpho Blue
}

# Seuils de qualification — Niveau 1 (screening institutionnel)
SCREENING_THRESHOLDS = {
    "min_tvl_usd":          500_000_000,   # TVL minimum : 500M$
    "max_tvl_drop_30d_pct": -30.0,         # Baisse max acceptable sur 30j : -30%
    "min_tvl_stability":    0.70,          # Ratio TVL min/max sur 30j : 70%
}

# ═══════════════════════════════════════════════════════════════════════════════
# NIVEAU 1 — SCREENING DEFILLAMA (5 PROTOCOLES)
# ═══════════════════════════════════════════════════════════════════════════════
def get_protocol_tvl_summary(protocol_slug: str) -> dict:
    """
    Récupère le résumé TVL d'un protocole via DeFi Llama.

    DeFi Llama fournit pour chaque protocole :
    - Le TVL total toutes chaînes confondues
    - La décomposition par chaîne (on isole Ethereum)
    - L'historique TVL sur 30/90 jours
    - Les revenus générés (fees)

    Args:
        protocol_slug : Identifiant DeFi Llama du protocole

    Returns:
        Dictionnaire avec les métriques clés
    """

    try:
        data = fetch_defillama(f"/protocol/{protocol_slug}")
    except requests.HTTPError as e:
        print(f"   ⚠️  Erreur API pour {protocol_slug} : {e}")
        return {}

    # TVL actuel toutes chaînes
    current_tvl_total = data.get("tvl", [{}])[-1].get("totalLiquidityUSD", 0) \
        if data.get("tvl") else 0

    # TVL Ethereum uniquement (chainTvls)
    chain_tvls = data.get("chainTvls", {})
    eth_tvl_series = chain_tvls.get("Ethereum", {}).get("tvl", [])
    current_tvl_eth = eth_tvl_series[-1].get("totalLiquidityUSD", 0) \
        if eth_tvl_series else 0

    # Historique TVL Ethereum sur 30 jours
    tvl_30d = eth_tvl_series[-30:] if len(eth_tvl_series) >= 30 else eth_tvl_series
    tvl_values = [p.get("totalLiquidityUSD", 0) for p in tvl_30d]

    tvl_30d_ago = tvl_values[0] if tvl_values else 0
    tvl_change_30d = (
        (current_tvl_eth / tvl_30d_ago - 1) * 100
        if tvl_30d_ago > 0 else 0
    )

    # Stabilité TVL : ratio min/max sur 30j (mesure la volatilité du TVL)
    # Un ratio proche de 1 indique un TVL stable — signal de confiance
    # Un ratio faible indique des retraits/dépôts massifs — signal d'instabilité
    tvl_stability = (
        min(tvl_values) / max(tvl_values)
        if tvl_values and max(tvl_values) > 0 else 0
    )

    # Revenus du protocole (fees annualisés)
    fees_data = data.get("fees", {})
    annual_revenue = fees_data.get("total30d", 0) * 12 \
        if fees_data.get("total30d") else 0

    return {
        "name": data.get("name", protocol_slug),
        "category": data.get("category", "N/A"),
        "tvl_total_usd": current_tvl_total,
        "tvl_eth_usd": current_tvl_eth,
        "tvl_change_30d_pct": tvl_change_30d,
        "tvl_stability_ratio": tvl_stability,
        "annual_revenue_usd": annual_revenue,
        "audit_links": data.get("audits", []),
        "description": data.get("description", "")[:100],
    }

def run_screening(protocols: dict) -> pd.DataFrame:
    """
    Niveau 1 : Screening institutionnel sur 5 protocoles de lending Ethereum.

    Ce screening applique des filtres quantitatifs simples pour identifier
    rapidement les protocoles éligibles à une due diligence approfondie.
    Il constitue la première étape d'un processus de sélection en deux temps,
    analogue à un premier filtre quantitatif en gestion d'actifs.

    Critères de qualification :
        1. TVL Ethereum > 500M$ : taille minimale pour une exposition institutionnelle
        2. Variation TVL 30j > -30% : pas de retrait massif récent
        3. Stabilité TVL > 70% : pas de volatilité structurelle

    Returns:
        DataFrame avec les résultats du screening et la décision d'éligibilité
    """
    print("\n" + "═" * 65)
    print("NIVEAU 1 — SCREENING DEFILLAMA : PROTOCOLES DE LENDING ETHEREUM")
    print("═" * 65)
    print(f"\n⏳ Interrogation de l'API DeFi Llama pour {len(protocols)} protocoles...\n")

    rows = []
    for name, slug in protocols.items():
        print(f"   Récupération : {name}...")
        summary = get_protocol_tvl_summary(slug)
        if summary:
            rows.append({"protocol": name, **summary})

    if not rows:
        print("⚠️  Aucune donnée disponible.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values("tvl_eth_usd", ascending=False).reset_index(drop=True)

    # Application des critères de qualification
    df["eligible"] = (
        (df["tvl_eth_usd"] >= SCREENING_THRESHOLDS["min_tvl_usd"]) &
        (df["tvl_change_30d_pct"] >= SCREENING_THRESHOLDS["max_tvl_drop_30d_pct"]) &
        (df["tvl_stability_ratio"] >= SCREENING_THRESHOLDS["min_tvl_stability"])
    )

    # Affichage du tableau de screening
    print(f"\n{'Protocole':<16} {'TVL ETH ($M)':>12} {'Var 30j':>9} {'Stabilité':>10} {'Revenu/an ($M)':>15} {'Éligible':>9}")
    print("─" * 75)

    for _, row in df.iterrows():
        eligible_icon = "✅" if row["eligible"] else "❌"   
        tvl_change_str = f"{row['tvl_change_30d_pct']:+.1f}%"
        tvl_change_colored = tvl_change_str

        print(
            f"{row['protocol']:<16} "
            f"${row['tvl_eth_usd']/1e6:>10.0f}M "
            f"{tvl_change_colored:>9} "
            f"{row['tvl_stability_ratio']:>9.1%} "
            f"${row['annual_revenue_usd']/1e6:>13.1f}M "
            f"   {eligible_icon}"
        )

    eligible_protocols = df[df["eligible"]]["protocol"].tolist()
    print(f"\n📋 Protocoles éligibles au Niveau 2 : {', '.join(eligible_protocols)}")
    print(f"\n   Critères appliqués :")
    print(f"   • TVL Ethereum minimum    : ${SCREENING_THRESHOLDS['min_tvl_usd']/1e6:.0f}M")
    print(f"   • Variation TVL 30j min   : {SCREENING_THRESHOLDS['max_tvl_drop_30d_pct']}%")
    print(f"   • Stabilité TVL minimum   : {SCREENING_THRESHOLDS['min_tvl_stability']:.0%}")

    return df

# ═══════════════════════════════════════════════════════════════════════════════
# NIVEAU 2 — ANALYSE APPROFONDIE VIA THE GRAPH (AAVE V3 + MORPHO)
# ═══════════════════════════════════════════════════════════════════════════════

def get_thegraph_lending_markets(protocol_name: str, subgraph_url: str) -> pd.DataFrame:
    """
    Récupère la composition des marchés de lending d'un protocole via The Graph.

    Cette analyse est commune à Aave V3 et Morpho, malgré leurs architectures
    différentes :
    - Aave V3 : marchés multi-actifs avec pools partagées
    - Morpho Blue : marchés isolés (chaque paire actif/collatéral est un marché
      indépendant), ce qui réduit le risque de contagion entre marchés mais
      fragmente la liquidité

    Cette différence architecturale est un critère de due diligence important
    pour un institutionnel : Morpho offre une meilleure isolation des risques
    mais une liquidité potentiellement plus fragmentée.

    Args:
        protocol_name : Nom du protocole pour l'affichage
        subgraph_url  : URL du subgraph The Graph

    Returns:
        DataFrame avec les métriques par marché
    """
    # Requête générique compatible Aave V3 et Morpho
    # (les deux utilisent le schéma Messari standard sur The Graph)
    query = """
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

    try:
        data = run_graphql_query(subgraph_url, query)
    except Exception as e:
        print(f"   ⚠️  Erreur subgraph {protocol_name} : {e}")
        return pd.DataFrame()

    markets = [m for m in data.get("markets", []) if m.get("isActive", True)]

    if not markets:
        return pd.DataFrame()

    total_tvl = sum(float(m["totalValueLockedUSD"]) for m in markets)
    rows = []

    for market in markets:
        tvl = float(market["totalValueLockedUSD"])
        deposits = float(market["totalDepositBalanceUSD"])
        borrows = float(market["totalBorrowBalanceUSD"])

        # Extraction du taux de dépôt variable
        supply_rate = 0.0
        for rate in market.get("rates", []):
            if rate.get("side") == "LENDER" and rate.get("type") == "VARIABLE":
                supply_rate = float(rate.get("rate", 0))
                break

        rows.append({
            "protocol": protocol_name,
            #"market": market["inputToken"]["symbol"],
            "market": market["name"],
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
    df["total_tvl_protocol"] = total_tvl

    return df


def get_tvl_history_defillama(protocol_slug: str, days: int = 30) -> pd.DataFrame:
    """
    Récupère l'historique TVL Ethereum d'un protocole via DeFi Llama.

    Utilisé en Niveau 2 pour une analyse temporelle plus fine que le simple
    résumé du screening. On isole spécifiquement la chaîne Ethereum pour
    éviter de diluer l'analyse avec les déploiements multi-chain.

    Args:
        protocol_slug : Identifiant DeFi Llama
        days          : Nombre de jours d'historique

    Returns:
        DataFrame avec l'historique TVL journalier
    """
    try:
        data = fetch_defillama(f"/protocol/{protocol_slug}")
    except Exception as e:
        print(f"   ⚠️  Erreur historique TVL : {e}")
        return pd.DataFrame()

    chain_tvls = data.get("chainTvls", {})
    eth_series = chain_tvls.get("Ethereum", {}).get("tvl", [])

    if not eth_series:
        return pd.DataFrame()

    recent = eth_series[-days:]
    rows = []
    for point in recent:
        rows.append({
            "date": datetime.utcfromtimestamp(point["date"]).strftime("%Y-%m-%d"),
            "tvl_usd": float(point["totalLiquidityUSD"]),
        })

    df = pd.DataFrame(rows)
    df["tvl_change_pct"] = df["tvl_usd"].pct_change() * 100

    return df


def analyze_protocol_deep(
    protocol_name: str,
    protocol_slug: str,
    subgraph_url: str,
) -> dict:
    """
    Niveau 2 : Analyse approfondie d'un protocole de lending.

    Combine l'historique TVL DeFi Llama et la composition des marchés
    via The Graph pour produire un scoring sur 4 dimensions.

    Args:
        protocol_name : Nom du protocole
        protocol_slug : Slug DeFi Llama
        subgraph_url  : URL du subgraph The Graph

    Returns:
        Dictionnaire avec scores et métriques détaillées
    """
    print(f"\n{'─' * 65}")
    print(f"ANALYSE APPROFONDIE — {protocol_name.upper()}")
    print(f"{'─' * 65}")

    # ── Historique TVL ─────────────────────────────────────────────────
    print(f"\n⏳ Récupération historique TVL (30j)...")
    tvl_df = get_tvl_history_defillama(protocol_slug, days=30)

    tvl_metrics = {}
    if not tvl_df.empty:
        current_tvl = tvl_df.iloc[-1]["tvl_usd"]
        tvl_30d_ago = tvl_df.iloc[0]["tvl_usd"]
        max_daily_drop = tvl_df["tvl_change_pct"].min()
        tvl_trend = (current_tvl / tvl_30d_ago - 1) * 100 if tvl_30d_ago > 0 else 0
        tvl_stability = tvl_df["tvl_usd"].min() / tvl_df["tvl_usd"].max() \
            if tvl_df["tvl_usd"].max() > 0 else 0

        tvl_metrics = {
            "current_usd": current_tvl,
            "trend_30d_pct": tvl_trend,
            "max_daily_drop_pct": max_daily_drop,
            "stability_ratio": tvl_stability,
        }

        print(f"\n📊 TVL actuel (Ethereum) : ${current_tvl/1e9:.2f} Mds")
        print(f"   Variation 30j         : {tvl_trend:+.1f}%")
        print(f"   Stabilité TVL         : {tvl_stability:.1%}")
        print(f"   Baisse journalière max : {max_daily_drop:.1f}%")

    # ── Composition des marchés ────────────────────────────────────────
    print(f"\n⏳ Récupération composition des marchés...")
    markets_df = get_thegraph_lending_markets(protocol_name, subgraph_url)

    concentration_metrics = {}
    if not markets_df.empty:
        total_tvl_protocol = markets_df["total_tvl_protocol"].iloc[0]

        # Calcul HHI (Herfindahl-Hirschman Index)
        hhi = sum(row["tvl_share_pct"] ** 2 for _, row in markets_df.iterrows())
        top3_share = markets_df.head(3)["tvl_share_pct"].sum()
        avg_utilization = markets_df["utilization_rate"].mean()
        high_util_markets = markets_df[markets_df["utilization_rate"] > 0.85]

        concentration_metrics = {
            "hhi": hhi,
            "top3_share_pct": top3_share,
            "avg_utilization": avg_utilization,
            "high_util_count": len(high_util_markets),
            "market_count": len(markets_df),
        }

        print(f"\n📊 Marchés actifs         : {len(markets_df)}")
        print(f"   TVL total protocole   : ${total_tvl_protocol/1e9:.2f} Mds")
        print(f"   HHI (concentration)   : {hhi:.0f}", end="  ")
        if hhi < 1500:
            print("✅ Peu concentré")
        elif hhi < 2500:
            print("⚠️  Modérément concentré")
        else:
            print("🚨 Très concentré")

        print(f"   Part top 3 actifs     : {top3_share:.1f}%")
        print(f"   Utilisation moyenne   : {avg_utilization:.1%}")
        if len(high_util_markets) > 0:
            print(f"   ⚠️  Marchés utilisation > 85% : {len(high_util_markets)}")
            for _, m in high_util_markets.iterrows():
                print(f"       → {m['market']} : {m['utilization_rate']:.1%}")

        print(f"\n📋 Top 8 marchés par TVL :")
        print(f"{'Actif':<12} {'TVL ($M)':>10} {'Part%':>7} {'Utilisation':>12} {'APY Supply':>11}")
        print("─" * 55)
        for _, row in markets_df.head(8).iterrows():
            print(
                f"{row['market']:<12} "
                f"${row['tvl_usd']/1e6:>8.1f}M "
                f"{row['tvl_share_pct']:>6.1f}% "
                f"{row['utilization_rate']:>11.1%} "
                f"{row['supply_apy_pct']:>10.2f}%"
            )

    # ── Scoring ────────────────────────────────────────────────────────
    scores = {}

    # Dimension 1 : Solidité du TVL
    if tvl_metrics:
        score = 4
        if tvl_metrics.get("current_usd", 0) < 500_000_000:
            score -= 2
        if tvl_metrics.get("max_daily_drop_pct", 0) < -20:
            score -= 1
        if tvl_metrics.get("trend_30d_pct", 0) < -15:
            score -= 1
        if tvl_metrics.get("stability_ratio", 1) < 0.70:
            score -= 1
        scores["tvl_solidity"] = max(1, min(4, score))

    # Dimension 2 : Diversification
    if concentration_metrics:
        hhi = concentration_metrics.get("hhi", 0)
        if hhi < 1500:
            scores["concentration"] = 4
        elif hhi < 2000:
            scores["concentration"] = 3
        elif hhi < 2500:
            scores["concentration"] = 2
        else:
            scores["concentration"] = 1

    # Dimension 3 : Santé opérationnelle
    if concentration_metrics:
        score = 4
        avg_util = concentration_metrics.get("avg_utilization", 0)
        high_util = concentration_metrics.get("high_util_count", 0)
        if avg_util > 0.90:
            score -= 2
        elif avg_util > 0.80:
            score -= 1
        if high_util >= 3:
            score -= 1
        scores["health"] = max(1, min(4, score))

    if scores:
        scores["global"] = round(sum(scores.values()) / len(scores), 2)

    return {
        "protocol": protocol_name,
        "scores": scores,
        "tvl_metrics": tvl_metrics,
        "concentration_metrics": concentration_metrics,
        "markets_df": markets_df,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RAPPORT COMPARATIF FINAL
# ═══════════════════════════════════════════════════════════════════════════════

def generate_comparative_report(results: list[dict]) -> None:
    """
    Génère le rapport comparatif final Aave V3 vs Morpho.

    Ce rapport synthétise les résultats du Niveau 2 sous forme d'un tableau
    comparatif directement exploitable pour une décision d'exposition
    institutionnelle. Il constitue la grille de scoring de la Section II.3
    du mémoire, appliquée à deux protocoles réels.

    Structure du scoring (1 = faible, 4 = fort) :
        1 — Solidité du TVL       : taille, stabilité, trend 30j
        2 — Diversification       : HHI, concentration par actif
        3 — Santé opérationnelle  : utilisation, marchés à risque

    Note : la dimension Gouvernance (Aave on-chain vs Morpho centralisé)
    est traitée séparément dans le Script 1 original (Aave) et constitue
    un critère différenciant majeur entre les deux protocoles.
    """
    print("\n" + "═" * 65)
    print("RAPPORT COMPARATIF — AAVE V3 vs MORPHO")
    print(f"Date d'analyse : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("═" * 65)

    rating_labels = {4: "FORT", 3: "SATISFAISANT", 2: "MODÉRÉ", 1: "FAIBLE"}
    dimension_labels = {
        "tvl_solidity": "Solidité du TVL",
        "concentration": "Diversification des actifs",
        "health":        "Santé opérationnelle",
    }

    # Tableau comparatif des scores
    print(f"\n{'Dimension':<30}", end="")
    for r in results:
        print(f"  {r['protocol']:<20}", end="")
    print()
    print("─" * (30 + 22 * len(results)))

    for dim, label in dimension_labels.items():
        print(f"{label:<30}", end="")
        for r in results:
            score = r["scores"].get(dim, 0)
            bar = "█" * score + "░" * (4 - score)
            print(f"  [{bar}] {score}/4         ", end="")
        print()

    print("─" * (30 + 22 * len(results)))
    print(f"{'SCORE GLOBAL':<30}", end="")
    for r in results:
        g = r["scores"].get("global", 0)
        print(f"  {g:.2f}/4.00              ", end="")
    print()

    # Verdicts individuels
    print(f"\n{'─' * 65}")
    print("VERDICTS :")
    for r in results:
        global_score = r["scores"].get("global", 0)
        if global_score >= 3.0:
            verdict = "✅ ÉLIGIBLE À L'EXPOSITION"
        elif global_score >= 2.0:
            verdict = "⚠️  EXPOSITION CONDITIONNELLE"
        else:
            verdict = "❌ NON ÉLIGIBLE"

        print(f"\n   {r['protocol']}")
        print(f"   Score global : {global_score:.2f}/4.00 — {verdict}")

        # Points saillants
        tvl = r["tvl_metrics"].get("current_usd", 0)
        trend = r["tvl_metrics"].get("trend_30d_pct", 0)
        hhi = r["concentration_metrics"].get("hhi", 0)
        util = r["concentration_metrics"].get("avg_utilization", 0)
        markets = r["concentration_metrics"].get("market_count", 0)

        print(f"   TVL Ethereum  : ${tvl/1e9:.2f} Mds ({trend:+.1f}% sur 30j)")
        print(f"   HHI           : {hhi:.0f} — {markets} marchés actifs")
        print(f"   Utilisation   : {util:.1%} moyenne")

    # Note méthodologique
    print(f"\n{'─' * 65}")
    print("NOTE MÉTHODOLOGIQUE :")
    print("""
   Ce scoring couvre les dimensions quantitatives on-chain.
   Une due diligence institutionnelle complète intègre également :

   • Audit de smart contracts (cf. Script 2 — Section II.2)
     Morpho Blue a été audité par Spearbit et Cantina (2023)
     Aave V3 a été audité par Trail of Bits, ABDK et SigmaPrime

   • Gouvernance : Aave dispose d'une gouvernance on-chain mature
     (token AAVE, snapshot + exécution on-chain via timelock)
     Morpho est gouverné par une multisig — plus centralisé

   • Risque de contrepartie SG Forge : Morpho est l'infrastructure
     utilisée par SG Forge pour ses opérations DeFi institutionnelles,
     ce qui constitue un signal de validation institutionnelle fort
    """)

def generate_finals_reports(screening_df: pd.DataFrame,deep_results: list,  )->None :
    # Export CSV du screening
    screening_df.to_csv("screening_lending_protocols.csv", index=False)
    print(f"\n💾 Screening exporté : screening_lending_protocols.csv")

    # ── Rapport comparatif final ───────────────────────────────────────
    generate_comparative_report(deep_results)

    # Export JSON du rapport final
    report = {
        "analysis_date": datetime.utcnow().isoformat(),
        "screening": {
            row["protocol"]: {
                "tvl_eth_usd": row["tvl_eth_usd"],
                "tvl_change_30d_pct": row["tvl_change_30d_pct"],
                "eligible": bool(row["eligible"]),
            }
            for _, row in screening_df.iterrows()
        },
        "deep_analysis": {
            r["protocol"]: {
                "scores": r["scores"],
                "tvl_metrics": r["tvl_metrics"],
            }
            for r in deep_results
        },
    }

    with open("lending_due_diligence_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n💾 Rapport complet exporté : lending_due_diligence_report.json")

# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":

    print("━" * 65)
    print("DUE DILIGENCE MULTI-PROTOCOLES — LENDING ETHEREUM")
    print("Screening DeFi Llama + Analyse approfondie Aave V3 / Morpho")
    print("Section II.1 — Mémoire DeFi Institutionnelle")
    print("━" * 65)


    # ── NIVEAU 1 : Screening DeFi Llama ───────────────────────────────
    screening_df = run_screening(PROTOCOLS_SCREENING)

    #if screening_df.empty:
    #    print("\n⚠️  Screening échoué — vérifier la connexion réseau.")
    #     exit(1)

    

    # ── NIVEAU 2 : Analyse approfondie Aave V3 + Morpho ───────────────
    print("\n\n" + "═" * 65)
    print("NIVEAU 2 — ANALYSE APPROFONDIE : AAVE V3 ET MORPHO")
    print("═" * 65)

    deep_results = []

    protocols_deep = {
        "Aave V3": ("aave-v3", SUBGRAPHS["Aave V3"]),
        "Morpho":  ("morpho",  SUBGRAPHS["Morpho"]),
    }

    for name, (slug, subgraph_url) in protocols_deep.items():
        result = analyze_protocol_deep(
            protocol_name=name,
            protocol_slug=slug,
            subgraph_url=subgraph_url,
        )
        deep_results.append(result)

    # ── FINAL : Generation Exports ───────────────
    generate_finals_reports(screening_df, deep_results)
    
    print("\n" + "━" * 65)
