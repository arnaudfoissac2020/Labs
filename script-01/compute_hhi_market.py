"""
Calcul du HHI — Marché Morpho Blue EURCV / cbBTC
Via The Graph — réseau décentralisé (gateway.thegraph.com)

Market ID : 0xb5f8d5554d85b782d7080314bba3544983755a75eb5c432f5eae1c47c6af4da4

PRÉREQUIS :
-----------
1. Clé API The Graph
   → Créer un compte sur https://thegraph.com/studio/
   → Onglet "API Keys" → créer une clé → copier dans .env

2. Subgraph ID Morpho Blue sur le réseau décentralisé
   → https://thegraph.com/explorer/ → rechercher "morpho blue"
   → Cliquer sur le subgraph Morpho → copier l'ID (format Qm... ou hash)
   → Ou utiliser directement le Deployment ID depuis le studio Morpho

Variables .env :
   GRAPH_API_KEY=<votre_clé_api_the_graph>
   MORPHO_SUBGRAPH_ID=<subgraph_id_morpho_blue>  # ex: Hj4PrUt8...
   RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>  # pour le fallback on-chain

FORMAT DE L'URL The Graph :
   https://gateway.thegraph.com/api/{API_KEY}/subgraphs/id/{SUBGRAPH_ID}

DIFFÉRENCE AVEC blue-api.morpho.org :
   - gateway.thegraph.com = réseau décentralisé d'indexeurs (multi-nœuds)
   - blue-api.morpho.org  = API centralisée Morpho Labs (single point of failure)
   - The Graph = données brutes on-chain indexées (vérifiables)
   - Morpho API = données enrichies calculées (APY, USD, etc.)

Dépendances : pip install requests web3 python-dotenv
"""

import os, json, requests, sys
from datetime import datetime, timezone
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

MARKET_ID = "0xb5f8d5554d85b782d7080314bba3544983755a75eb5c432f5eae1c47c6af4da4"

# Clé API The Graph (https://thegraph.com/studio/ → API Keys)
GRAPH_API_KEY = os.getenv("GRAPH_API_KEY", "")

# Subgraph ID Morpho Blue sur le réseau décentralisé The Graph
# À récupérer sur https://thegraph.com/explorer/ → rechercher "morpho blue"
# Format : chaîne de 46 caractères commençant par "Qm" (IPFS CID)
# ou hash hexadécimal selon la version
MORPHO_SUBGRAPH_ID = os.getenv(
    "MORPHO_SUBGRAPH_ID"
)

# URL de la gateway The Graph
GRAPH_GATEWAY_URL = (
    f"https://gateway.thegraph.com/api/{GRAPH_API_KEY}"
    f"/subgraphs/id/{MORPHO_SUBGRAPH_ID}"
)

# Fallback on-chain
RPC_URL            = os.getenv("RPC_URL", "https://eth.llamarpc.com")
MORPHO_ADDRESS     = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Seuils HHI (DoJ/FTC Horizontal Merger Guidelines, 2010)
SEUIL_PEU_CONCENTRE = 0.15
SEUIL_CONCENTRE     = 0.25

# Pagination The Graph : max 1000 entrées par requête
PAGE_SIZE = 1000


# ═══════════════════════════════════════════════════════════════════════════════
# VÉRIFICATION DE LA CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

def verifier_configuration() -> bool:
    """
    Vérifie que la clé API et le subgraph ID sont configurés.
    Affiche les instructions si ce n'est pas le cas.
    """
    if not GRAPH_API_KEY:
        print("\n❌ GRAPH_API_KEY manquant dans le fichier .env")
        print("\n  COMMENT OBTENIR UNE CLÉ API THE GRAPH :")
        print("  1. Aller sur https://thegraph.com/studio/")
        print("  2. Créer un compte (gratuit)")
        print("  3. Onglet 'API Keys' → 'Create API Key'")
        print("  4. Copier la clé dans .env : GRAPH_API_KEY=<votre_clé>")
        print("\n  NOTE : Le réseau décentralisé The Graph est payant en GRT.")
        print("  Le gateway offre un quota gratuit pour les requêtes de test.")
        return False

    if not MORPHO_SUBGRAPH_ID:
        print("\n❌ MORPHO_SUBGRAPH_ID manquant dans le fichier .env")
        print("\n  COMMENT TROUVER L'ID DU SUBGRAPH MORPHO BLUE :")
        print("  1. Aller sur https://thegraph.com/explorer/")
        print("  2. Rechercher 'Morpho Blue' ou 'morpho-blue'")
        print("  3. Sélectionner le subgraph pour Ethereum Mainnet")
        print("  4. Copier l'ID affiché (onglet 'Deployment Details')")
        print("  5. Ajouter dans .env : MORPHO_SUBGRAPH_ID=<id>")
        return False

    return True


# ═══════════════════════════════════════════════════════════════════════════════
# REQUÊTES THE GRAPH (gateway.thegraph.com)
# ═══════════════════════════════════════════════════════════════════════════════

def requete_graph(query: str, variables: dict ) -> dict :
    """
    Envoie une requête GraphQL au subgraph Morpho Blue via le réseau
    décentralisé The Graph.

    Le gateway route automatiquement la requête vers les indexeurs
    disponibles sur le réseau décentralisé.

    Args:
        query     : requête GraphQL
        variables : variables de la requête

    Returns:
        Données retournées, ou None en cas d'erreur
    """
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        resp = requests.post(
            GRAPH_GATEWAY_URL,
            json=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {GRAPH_API_KEY}",
            },
            timeout=30,
        )

        # Gestion des erreurs HTTP spécifiques à The Graph
        if resp.status_code == 401:
            print("  ❌ Clé API invalide ou expirée — vérifier GRAPH_API_KEY")
            return None
        if resp.status_code == 404:
            print("  ❌ Subgraph non trouvé — vérifier MORPHO_SUBGRAPH_ID")
            return None
        if resp.status_code == 429:
            print("  ❌ Rate limit atteint — attendre avant de réessayer")
            return None

        resp.raise_for_status()
        data = resp.json()

        if "errors" in data:
            print(f"  ⚠️  Erreur GraphQL : {data['errors'][0].get('message', data['errors'])}")
            return None

        return data.get("data")

    except requests.exceptions.RequestException as e:
        print(f"  ❌ Erreur réseau : {e}")
        return None


def lire_infos_marche(market_id: str) -> dict :
    """
    Lit les informations globales du marché depuis The Graph.

    NOTE SUR LE SCHÉMA THE GRAPH vs MORPHO API :
    - The Graph utilise les adresses en minuscules sans checksum
    - Les shares et assets sont des strings (BigInt en GraphQL)
    - Pas de champ 'utilization' calculé — on le calcule soi-même
    - Le market ID dans The Graph est le bytes32 hex en minuscules
    """
    market_id_lower = market_id.lower()

    query = """
    query GetMarket($id: ID!) {
      market(id: $id) {
        id
        loanToken {
          id
          symbol
          decimals
        }
        collateralToken {
          id
          symbol
          decimals
        }
        lltv
        totalSupplyAssets
        totalSupplyShares
        totalBorrowAssets
        totalBorrowShares
        lastUpdate
      }
    }
    """

    data = requete_graph(query, {"id": market_id_lower})

    if not data:
        return None

    if not data.get("market"):
        # The Graph stocke parfois les IDs sans le préfixe 0x
        data = requete_graph(query, {"id": market_id_lower.lstrip("0x")})
        if not data or not data.get("market"):
            return None

    return data["market"]


def lire_toutes_positions(market_id: str) -> list[dict]:
    """
    Récupère toutes les positions de dépôt (supplyShares > 0)
    pour le marché cible via The Graph.

    Pagination via le champ 'id_gt' (cursor-based) :
    plus fiable que skip/first pour les grands jeux de données,
    car skip peut produire des doublons si des positions sont
    ajoutées pendant la pagination.

    Structure retournée par The Graph :
      positions {
        id          → "{market_id}-{wallet_address}"
        account     → { id: "0x..." }
        supplyShares → "123456789" (string BigInt)
        borrowShares → "0"
        collateral   → "0"
      }
    """
    market_id_lower = market_id.lower()
    positions = []
    last_id   = ""
    page      = 0

    # La requête The Graph filtre par marché ET par supplyShares > 0
    # L'ordre par 'id' garantit la cohérence de la pagination par curseur
    query = """
    query GetPositions($market: String!, $first: Int!, $lastId: ID!) {
      positions(
        where: {
          market:        $market,
          supplyShares_gt: "0",
          id_gt:         $lastId
        }
        first:          $first
        orderBy:        id
        orderDirection: asc
      ) {
        id
        account {
          id
        }
        supplyShares
        borrowShares
        collateral
      }
    }
    """

    while True:
        page += 1
        data = requete_graph(query, {
            "market":  market_id_lower,
            "first":   PAGE_SIZE,
            "lastId":  last_id,
        })

        if not data or not data.get("positions"):
            if page == 1:
                print("  ⚠️  Aucune position retournée par The Graph.")
                print("     Vérifier que le subgraph indexe bien ce marché.")
            break

        batch = data["positions"]
        positions.extend(batch)
        print(f"  Page {page} — {len(batch)} position(s) "
              f"(total cumulé : {len(positions)})")

        # Fin de pagination si moins d'entrées que la page size
        if len(batch) < PAGE_SIZE:
            break

        # Curseur : dernier ID pour la prochaine page
        last_id = batch[-1]["id"]

    return positions


# ═══════════════════════════════════════════════════════════════════════════════
# FALLBACK ON-CHAIN (Web3.py + Multicall3)
# ═══════════════════════════════════════════════════════════════════════════════

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.morpho_abis import MORPHO_ABI, MORPHO_EVENTS_ABI, MULTICALL3_ABI",
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


def fallback_onchain(market_id: str) -> dict :
    """
    Fallback on-chain si The Graph est inaccessible.

    Étapes :
    1. Identifier les déposants via les événements Supply/Withdraw on-chain
    2. Lire les supplyShares en batch via Multicall3 (un seul appel RPC)
    3. Retourner les positions actives (supplyShares > 0)

    NOTE : Cette approche est source de vérité absolue (on-chain),
    mais la fenêtre d'événements est limitée aux derniers N blocs.
    Pour un historique complet, augmenter FENETRE ou utiliser un
    archive node (Alchemy, Infura avec plan Archive).
    """
    print("\n  Fallback on-chain (Web3.py + Multicall3)...")

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("  ❌ Connexion RPC indisponible — vérifier RPC_URL dans .env")
        return None

    bloc_actuel    = w3.eth.block_number
    market_bytes   = bytes.fromhex(market_id[2:])
    morpho         = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_ADDRESS),
        abi=MORPHO_ABI + MORPHO_EVENTS_ABI
    )

    print(f"  ✅ RPC connecté — Bloc #{bloc_actuel:,}")

    # Lire l'état global du marché
    mkt = morpho.functions.market(market_bytes).call()
    total_supply_shares = mkt[1]

    # Identifier les déposants via événements (fenêtre ~7 jours = 50 000 blocs)
    FENETRE   = 50_000
    deponents = set()

    for evt_name in ["Supply", "Withdraw"]:
        try:
            ef = morpho.events[evt_name].create_filter(
                from_block=max(0, bloc_actuel - FENETRE),
                to_block=bloc_actuel,
                argument_filters={"id": market_bytes}
            )
            for evt in ef.get_all_entries():
                deponents.add(evt["args"]["onBehalf"])
        except Exception as e:
            print(f"  ⚠️  Événements {evt_name} : {e}")

    if not deponents:
        print("  ⚠️  Aucun déposant trouvé sur la fenêtre de blocs.")
        print("     Augmenter FENETRE ou utiliser un archive node.")
        return None

    print(f"  {len(deponents)} adresse(s) identifiée(s)")
    print(f"  Lecture batch des positions via Multicall3...")

    # Batch lecture via Multicall3 — un seul appel RPC
    multicall      = w3.eth.contract(
        address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
        abi=MULTICALL3_ABI
    )
    deponents_list = list(deponents)
    calls          = [
        (
            Web3.to_checksum_address(MORPHO_ADDRESS),
            morpho.encode_abi(
                "position",
                args=[market_bytes, Web3.to_checksum_address(addr)]
            )
        )
        for addr in deponents_list
    ]

    _, return_data = multicall.functions.aggregate(calls).call()

    positions = []
    for addr, raw in zip(deponents_list, return_data):
        decoded       = w3.codec.decode(["uint256", "uint128", "uint128"], raw)
        supply_shares = decoded[0]
        if supply_shares > 0:
            positions.append({
                "wallet":        addr,
                "supplyShares":  str(supply_shares),
            })

    print(f"  {len(positions)} position(s) active(s)")

    return {
        "source":               "On-chain (Web3.py + Multicall3)",
        "total_supply_shares":  total_supply_shares,
        "positions":            positions,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CALCUL DU HHI
# ═══════════════════════════════════════════════════════════════════════════════

def calculer_hhi(positions: list[dict], total_supply_shares: int) -> dict:
    """
    Calcule le HHI (Herfindahl-Hirschman Index) de concentration des dépôts.

    Formule : HHI = Σ (supplyShares_i / totalSupplyShares)²

    Référence : U.S. DoJ & FTC, Horizontal Merger Guidelines, 2010, Section 5.3
    https://www.justice.gov/atr/horizontal-merger-guidelines-08192010
    """
    if not positions or total_supply_shares == 0:
        return {"erreur": "Données insuffisantes pour calculer le HHI"}

    # Normalisation du champ supplyShares
    # (The Graph retourne des strings, fallback on-chain retourne des ints)
    parts = []
    for p in positions:
        shares_raw = p.get("supplyShares") or p.get("supply_shares", 0)
        shares     = int(shares_raw)
        if shares > 0:
            part = shares / total_supply_shares
            # The Graph peut retourner account.id ou wallet
            wallet = (p.get("account", {}).get("id")
                      if isinstance(p.get("account"), dict)
                      else p.get("wallet", "unknown"))
            parts.append({
                "wallet":            wallet,
                "supply_shares":     shares,
                "part_pct":          round(part * 100, 4),
                "contribution_hhi":  round(part ** 2, 8),
            })

    # Tri par part décroissante
    parts.sort(key=lambda x: x["supply_shares"], reverse=True)

    hhi = sum(p["contribution_hhi"] for p in parts)

    # Classification DoJ/FTC
    if hhi < SEUIL_PEU_CONCENTRE:
        classif, statut = "PEU CONCENTRÉ",          "✅"
        interpretation  = ("Marché bien diversifié — risque de retrait massif "
                           "par un seul acteur faible.")
    elif hhi < SEUIL_CONCENTRE:
        classif, statut = "MODÉRÉMENT CONCENTRÉ",   "⚠️"
        interpretation  = ("Concentration modérée — surveiller les mouvements "
                           "des principaux déposants.")
    else:
        classif, statut = "CONCENTRÉ",              "🚨"
        interpretation  = ("Marché fortement concentré — un retrait d'un grand "
                           "déposant peut perturber la liquidité et les taux.")

    return {
        "hhi":            round(hhi, 6),
        "classification": classif,
        "statut":         statut,
        "interpretation": interpretation,
        "nb_deponents":   len(parts),
        "top_3_pct":      round(sum(p["part_pct"] for p in parts[:3]),  2),
        "top_5_pct":      round(sum(p["part_pct"] for p in parts[:5]),  2),
        "top_10_pct":     round(sum(p["part_pct"] for p in parts[:10]), 2),
        "top_deponents":  parts[:10],
        "detail_complet": parts,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AFFICHAGE DU RAPPORT
# ═══════════════════════════════════════════════════════════════════════════════

def afficher_rapport(
    source: str,
    infos_marche: dict ,
    hhi: dict,
) -> None:

    print(f"\n{'═'*62}")
    print(f"RAPPORT HHI — MARCHÉ MORPHO BLUE EURCV / cbBTC")
    print(f"{'═'*62}")
    print(f"  Source : {source}")

    # Informations du marché (si disponibles depuis The Graph)
    if infos_marche:
        loan = infos_marche.get("loanToken", {})
        coll = infos_marche.get("collateralToken", {})
        ts_s = infos_marche.get("totalSupplyAssets", 0)
        tb_s = infos_marche.get("totalBorrowAssets", 0)
        lltv = int(infos_marche.get("lltv", 0)) / 1e18 * 100
        loan_dec = int(loan.get("decimals", 18))
        tvl = int(ts_s) / (10**loan_dec) if ts_s else 0
        utilisation = (int(tb_s) / int(ts_s) * 100
                       if ts_s and int(ts_s) > 0 else 0)

        print(f"\n  Marché      : {loan.get('symbol','?')} / "
              f"{coll.get('symbol','?')}")
        print(f"  LLTV        : {lltv:.1f}%")
        print(f"  TVL         : {tvl:,.2f} {loan.get('symbol','?')}")
        print(f"  Utilisation : {utilisation:.2f}%")

    if "erreur" in hhi:
        print(f"\n  ❌ {hhi['erreur']}")
        return

    print(f"\n{'─'*62}")
    print(f"  HHI calculé      : {hhi['hhi']:.6f}")
    print(f"  Classification   : {hhi['statut']}  {hhi['classification']}")
    print(f"  Nb déposants     : {hhi['nb_deponents']}")
    print(f"\n  CONCENTRATION CUMULÉE :")
    print(f"    Top  3 déposants : {hhi['top_3_pct']:>7.2f}%")
    print(f"    Top  5 déposants : {hhi['top_5_pct']:>7.2f}%")
    print(f"    Top 10 déposants : {hhi['top_10_pct']:>7.2f}%")

    print(f"\n  TOP 10 DÉPOSANTS :")
    print(f"  {'#':<3} {'Wallet':<44} {'Part':>8}  {'HHI contrib':>12}")
    print(f"  {'─'*74}")
    for i, d in enumerate(hhi["top_deponents"], 1):
        ws = d['wallet'][:6] + "..." + d['wallet'][-4:]
        print(f"  {i:<3} {ws:<44} "
              f"{d['part_pct']:>7.4f}%  "
              f"{d['contribution_hhi']:>12.8f}")

    print(f"\n  INTERPRÉTATION : {hhi['interpretation']}")
    print(f"\n  SEUILS DoJ/FTC (Horizontal Merger Guidelines, 2010) :")
    print(f"    HHI < 0.15  → peu concentré     ✅")
    print(f"    0.15 – 0.25 → modérément conc.  ⚠️")
    print(f"    HHI > 0.25  → concentré         🚨")
    print(f"\n  ⚠️  The Graph = source off-chain indexée (données brutes on-chain).")
    print(f"     Pour validation officielle : croiser avec Multicall3 on-chain.")


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("HHI — MARCHÉ MORPHO BLUE EURCV / cbBTC")
    print("The Graph (gateway.thegraph.com) avec fallback on-chain")
    print(f"Market ID : {MARKET_ID}")
    print("━"*62)

    # ── Vérification de la configuration ──────────────────────────────────────
    if not verifier_configuration():
        print("\n  Utilisation du fallback on-chain...")
        data = fallback_onchain(MARKET_ID)
        source = "On-chain (Web3.py + Multicall3)"
        infos_marche = None
    else:
        print(f"\n  Subgraph ID : {MORPHO_SUBGRAPH_ID}")
        print(f"  Gateway URL : {GRAPH_GATEWAY_URL[:60]}...")

        # ── Étape 1 : Informations du marché ──────────────────────────────────
        print("\n[1/3] Lecture des informations du marché via The Graph...")
        infos_marche = lire_infos_marche(MARKET_ID)

        if infos_marche:
            total_supply_shares = int(
                infos_marche.get("totalSupplyShares", 0)
            )
            loan = infos_marche.get("loanToken", {})
            coll = infos_marche.get("collateralToken", {})
            print(f"  ✅ {loan.get('symbol','?')} / {coll.get('symbol','?')}")
        else:
            print("  ⚠️  Marché non trouvé — basculement sur le fallback on-chain")
            data = fallback_onchain(MARKET_ID)
            source = "On-chain (Web3.py + Multicall3)"
            infos_marche = None
            total_supply_shares = data["total_supply_shares"] if data else 0

        if infos_marche:
            # ── Étape 2 : Positions des déposants ─────────────────────────────
            print("\n[2/3] Récupération des positions via The Graph...")
            positions_raw = lire_toutes_positions(MARKET_ID)

            if positions_raw:
                print(f"  ✅ {len(positions_raw)} position(s) récupérée(s)")
                data = {
                    "source":              "The Graph (gateway.thegraph.com)",
                    "total_supply_shares": total_supply_shares,
                    "positions":           positions_raw,
                }
                source = "The Graph (gateway.thegraph.com)"
            else:
                print("  ⚠️  The Graph n'a retourné aucune position.")
                print("     Basculement sur le fallback on-chain...")
                data   = fallback_onchain(MARKET_ID)
                source = "On-chain (Web3.py + Multicall3)"
                infos_marche = None

    if not data:
        print("\n❌ Impossible de récupérer les données.")
        print("   Vérifier GRAPH_API_KEY, MORPHO_SUBGRAPH_ID et RPC_URL.")
        exit(1)

    # ── Étape 3 : Calcul du HHI ───────────────────────────────────────────────
    print("\n[3/3] Calcul du HHI...")
    hhi = calculer_hhi(
        data["positions"],
        data["total_supply_shares"]
    )

    # ── Rapport ───────────────────────────────────────────────────────────────
    afficher_rapport(source, infos_marche, hhi)

    # ── Export JSON ───────────────────────────────────────────────────────────
    export = {
        "timestamp":         datetime.now(tz=timezone.utc).isoformat(),
        "market_id":         MARKET_ID,
        "source":            source,
        "subgraph_id":       MORPHO_SUBGRAPH_ID if GRAPH_API_KEY else None,
        "hhi":               {k: v for k, v in hhi.items()
                              if k != "detail_complet"},
        "top_deponents":     hhi.get("top_deponents", []),
    }

    with open("hhi_eurcv_cbbtc.json", "w") as f:
        json.dump(export, f, indent=2, default=str)

    print(f"\n💾 Rapport exporté : hhi_eurcv_cbbtc.json")
    print("━"*62)