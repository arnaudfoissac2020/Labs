"""
SCRIPT 8 — Réconciliation des positions Morpho Blue
          On-chain vs systèmes internes (OMS/TMS)

Contexte : La réconciliation est l'une des missions critiques du middle-office.
           Pour des positions Morpho Blue, elle présente deux spécificités :
           1. La source de vérité est on-chain — publique, immuable et
              non falsifiable. Elle constitue la référence absolue.
           2. Les intérêts s'accumulent en continu (~7 200 fois/jour), ce
              qui génère des micro-écarts résiduels entre le calcul EOD
              du TMS et la valeur on-chain instantanée.

           Ce script conduit la réconciliation à deux niveaux :

           Niveau 1 — Positions nominales :
           Vérification que les montants de supply/borrow enregistrés dans
           le TMS correspondent aux positions on-chain lues via le contrat
           Morpho Blue (idToMarketParams + position + market).

           Niveau 2 — Intérêts accumulés :
           Comparaison entre l'accrual quotidien calculé par le TMS (via
           le taux contractuel stocké) et l'accrual réel on-chain (via le
           ratio totalAssets/totalShares entre t₀ et t₁).

           Seuils de tolérance aux écarts :
           - OK         : < 0.01% (arrondi de conversion décimale)
           - ACCEPTABLE : 0.01% — 0.10% (micro-rounding accrual per-block)
           - INVESTIGATION : 0.10% — 0.50% (écart significatif à analyser)
           - CRITIQUE   : > 0.50% (erreur de booking possible)

Dépendances :
    pip install web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
  - Morpho Docs, Variable Rate Market, 2025
    https://docs.morpho.org/learn/concepts/market/
  - FSB, The Financial Stability Risks of DeFi, 2023
    https://www.fsb.org/2023/02/the-financial-stability-risks-of-decentralised-finance/
"""

import os, sys
import json
import csv
from datetime import datetime, timezone
from enum import Enum
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── ADRESSES ─────────────────────────────────────────────────────────────────

MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

TOKEN_INFO = {
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": {"symbol": "USDC",   "decimals": 6},
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": {"symbol": "USDT",   "decimals": 6},
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": {"symbol": "WETH",   "decimals": 18},
    "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0": {"symbol": "wstETH", "decimals": 18},
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {"symbol": "WBTC",   "decimals": 8},
    "0x6B175474E89094C44Da98b954EedeAC495271d0F": {"symbol": "DAI",    "decimals": 18},
}

# ─── SEUILS DE RÉCONCILIATION ─────────────────────────────────────────────────

class StatutEcart(Enum):
    OK             = "✅ OK"
    ACCEPTABLE     = "ℹ️  ACCEPTABLE"
    INVESTIGATION  = "⚠️  INVESTIGATION"
    CRITIQUE       = "🚨 CRITIQUE"

SEUILS_ECART_PCT = {
    StatutEcart.OK:            0.01,    # < 0.01%
    StatutEcart.ACCEPTABLE:    0.10,    # 0.01% — 0.10%
    StatutEcart.INVESTIGATION: 0.50,    # 0.10% — 0.50%
    # > 0.50% → CRITIQUE
}

SEUIL_ABSOLU_CRITIQUE = {
    "USDC": 100.0,      # > 100 USDC en écart absolu → critique
    "USDT": 100.0,
    "WETH":   0.05,     # > 0.05 WETH
    "wstETH": 0.05,
    "WBTC":   0.001,    # > 0.001 WBTC
    "DEFAULT": 100.0,
}

# ─── ABIs ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.morpho_abis import MORPHO_ABI

# ═══════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def shares_to_assets(shares: int, total_assets: int, total_shares: int) -> int:
    """Convertit des shares en actifs (arrondi vers le bas — côté prêteur)."""
    if total_shares == 0:
        return 0
    return shares * total_assets // total_shares


def classifier_ecart(ecart_pct: float, ecart_absolu: float, token: str) -> StatutEcart:
    """
    Classe l'écart selon les seuils de tolérance définis.

    Deux critères combinés :
    1. Ecart relatif (%) — sensible aux petites positions
    2. Ecart absolu (unités de token) — critique pour les grandes positions
       même si le % est faible
    """
    seuil_abs = SEUIL_ABSOLU_CRITIQUE.get(token, SEUIL_ABSOLU_CRITIQUE["DEFAULT"])
    abs_critique = ecart_absolu > seuil_abs

    if abs_critique or ecart_pct > 0.50:
        return StatutEcart.CRITIQUE
    elif ecart_pct > 0.10:
        return StatutEcart.INVESTIGATION
    elif ecart_pct > 0.01:
        return StatutEcart.ACCEPTABLE
    else:
        return StatutEcart.OK


def calculer_ecart(valeur_onchain: float, valeur_tms: float) -> dict:
    """
    Calcule l'écart entre la valeur on-chain (source de vérité)
    et la valeur enregistrée dans le TMS.

    Convention :
    - Écart positif : le TMS sous-estime la valeur on-chain
      (intérêts non encore accrués dans le TMS)
    - Écart négatif : le TMS sur-estime la valeur on-chain
      (erreur de booking potentielle)
    """
    ecart_absolu = valeur_onchain - valeur_tms
    ecart_pct    = (abs(ecart_absolu) / valeur_onchain * 100
                    if valeur_onchain != 0 else 0.0)
    return {
        "ecart_absolu": round(ecart_absolu, 8),
        "ecart_pct":    round(ecart_pct, 6),
        "sens":         "TMS_SOUS_ESTIME" if ecart_absolu > 0 else
                        ("TMS_SUR_ESTIME" if ecart_absolu < 0 else "EXACT"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LECTURE ON-CHAIN DES POSITIONS
# ═══════════════════════════════════════════════════════════════════════════════

def lire_positions_onchain(wallet: str, market_ids: list[str]) -> list[dict]:
    """
    Lit toutes les positions Morpho Blue d'un wallet pour une liste de marchés.

    Pour chaque marché, retourne :
    - Les shares de supply, borrow, collateral (valeurs brutes on-chain)
    - Les assets équivalents (calculés via shares × totalAssets/totalShares)
    - Les paramètres immuables du marché

    Args:
        wallet     : adresse du wallet institutionnel
        market_ids : liste des market IDs en hex (bytes32)

    Returns:
        Liste des positions on-chain normalisées
    """
    morpho    = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI
    )
    wallet_cs = Web3.to_checksum_address(wallet)
    positions = []

    for market_id_hex in market_ids:
        try:
            market_id_bytes = bytes.fromhex(market_id_hex[2:])

            # Paramètres immuables
            params = morpho.functions.idToMarketParams(market_id_bytes).call()
            loan_token, collateral_token = params[0], params[1]
            lltv = params[4] / 1e18

            loan_info = TOKEN_INFO.get(
                Web3.to_checksum_address(loan_token),
                {"symbol": "UNKNOWN", "decimals": 18}
            )
            coll_info = TOKEN_INFO.get(
                Web3.to_checksum_address(collateral_token),
                {"symbol": "UNKNOWN", "decimals": 18}
            )

            # État global du marché
            mkt = morpho.functions.market(market_id_bytes).call()
            total_supply_assets = mkt[0]
            total_supply_shares = mkt[1]
            total_borrow_assets = mkt[2]
            total_borrow_shares = mkt[3]

            # Position du wallet
            pos = morpho.functions.position(
                market_id_bytes, wallet_cs
            ).call()
            supply_shares = pos[0]
            borrow_shares = pos[1]
            collateral    = pos[2]

            # Conversion shares → assets
            supply_assets_raw = shares_to_assets(
                supply_shares, total_supply_assets, total_supply_shares
            )
            borrow_assets_raw = shares_to_assets(
                borrow_shares, total_borrow_assets, total_borrow_shares
            )

            # Conversion en unités lisibles
            ld = loan_info["decimals"]
            cd = coll_info["decimals"]

            positions.append({
                "market_id":        market_id_hex,
                "loan_symbol":      loan_info["symbol"],
                "loan_decimals":    ld,
                "collateral_symbol": coll_info["symbol"],
                "coll_decimals":    cd,
                "lltv":             lltv,

                # Valeurs on-chain brutes (shares)
                "supply_shares_raw":  supply_shares,
                "borrow_shares_raw":  borrow_shares,
                "collateral_raw":     collateral,

                # Valeurs on-chain converties (assets)
                "supply_assets":  supply_assets_raw / (10**ld),
                "borrow_assets":  borrow_assets_raw / (10**ld),
                "collateral":     collateral / (10**cd),

                # Métriques du marché
                "utilisation_pct": (
                    total_borrow_assets / total_supply_assets * 100
                    if total_supply_assets > 0 else 0
                ),
                "tvl":  total_supply_assets / (10**ld),
            })

        except Exception as e:
            positions.append({
                "market_id": market_id_hex,
                "erreur":    str(e),
            })

    return positions


# ═══════════════════════════════════════════════════════════════════════════════
# RÉCONCILIATION NIVEAU 1 — POSITIONS NOMINALES
# ═══════════════════════════════════════════════════════════════════════════════

def reconcilier_positions_nominales(
    positions_onchain: list[dict],
    positions_tms: list[dict],
) -> list[dict]:
    """
    Réconciliation Niveau 1 : comparaison des montants nominaux entre
    la source on-chain (vérité absolue) et le TMS.

    TYPES D'ÉCARTS ET LEURS CAUSES TYPIQUES :
    - Écart < 0.01% : arrondi de conversion décimale — NORMAL
    - Écart 0.01-0.10% : micro-accrual entre le dernier EOD et maintenant — NORMAL
    - Écart 0.10-0.50% : retard d'intégration d'une transaction récente
      (withdraw ou supply exécuté mais non encore booké dans le TMS)
    - Écart > 0.50% : erreur de booking probable — à investiguer en priorité

    Args:
        positions_onchain : positions lues on-chain (cf. lire_positions_onchain)
        positions_tms     : positions extraites du TMS (format normalisé)

    Returns:
        Liste des résultats de réconciliation avec statut par ligne
    """
    resultats = []

    # Index les positions TMS par market_id pour un accès rapide
    tms_index = {p["market_id"]: p for p in positions_tms}

    for pos_oc in positions_onchain:
        market_id = pos_oc["market_id"]

        if "erreur" in pos_oc:
            resultats.append({
                "market_id": market_id,
                "statut":    StatutEcart.CRITIQUE.value,
                "erreur":    pos_oc["erreur"],
            })
            continue

        pos_tms = tms_index.get(market_id)
        loan    = pos_oc["loan_symbol"]
        coll    = pos_oc["collateral_symbol"]

        # ── Supply ─────────────────────────────────────────────────────────────
        supply_oc  = pos_oc["supply_assets"]
        supply_tms = pos_tms["supply_assets"] if pos_tms else 0.0
        ecart_supply = calculer_ecart(supply_oc, supply_tms)
        statut_supply = classifier_ecart(
            ecart_supply["ecart_pct"], abs(ecart_supply["ecart_absolu"]), loan
        )

        # ── Borrow ─────────────────────────────────────────────────────────────
        borrow_oc  = pos_oc["borrow_assets"]
        borrow_tms = pos_tms["borrow_assets"] if pos_tms else 0.0
        ecart_borrow = calculer_ecart(borrow_oc, borrow_tms)
        statut_borrow = classifier_ecart(
            ecart_borrow["ecart_pct"], abs(ecart_borrow["ecart_absolu"]), loan
        )

        # ── Collatéral ─────────────────────────────────────────────────────────
        coll_oc  = pos_oc["collateral"]
        coll_tms = pos_tms["collateral"] if pos_tms else 0.0
        ecart_coll = calculer_ecart(coll_oc, coll_tms)
        statut_coll = classifier_ecart(
            ecart_coll["ecart_pct"], abs(ecart_coll["ecart_absolu"]), coll
        )

        # ── Statut global de la ligne ──────────────────────────────────────────
        statuts = [statut_supply, statut_borrow, statut_coll]
        statut_global = max(statuts, key=lambda s: list(StatutEcart).index(s))

        resultats.append({
            "market_id":    market_id,
            "marche":       f"{loan}/{coll} (LLTV {pos_oc['lltv']*100:.1f}%)",
            "tms_present":  pos_tms is not None,

            "supply": {
                "onchain":    round(supply_oc,  6),
                "tms":        round(supply_tms, 6),
                "devise":     loan,
                **ecart_supply,
                "statut":     statut_supply.value,
            },
            "borrow": {
                "onchain":    round(borrow_oc,  6),
                "tms":        round(borrow_tms, 6),
                "devise":     loan,
                **ecart_borrow,
                "statut":     statut_borrow.value,
            },
            "collateral": {
                "onchain":    round(coll_oc,  6),
                "tms":        round(coll_tms, 6),
                "devise":     coll,
                **ecart_coll,
                "statut":     statut_coll.value,
            },

            "statut_global":  statut_global.value,
            "utilisation_pct": pos_oc.get("utilisation_pct"),
        })

    return resultats


# ═══════════════════════════════════════════════════════════════════════════════
# RÉCONCILIATION NIVEAU 2 — INTÉRÊTS ACCUMULÉS
# ═══════════════════════════════════════════════════════════════════════════════

def reconcilier_interets(
    wallet: str,
    market_id_hex: str,
    interets_tms: float,
    loan_symbol: str,
    bloc_reference: int | None = None,
) -> dict:
    """
    Réconciliation Niveau 2 : comparaison de l'accrual d'intérêts calculé
    par le TMS versus l'accrual réel on-chain depuis le dernier EOD.

    CAUSE DES MICRO-ÉCARTS (0.01-0.10%) :
    Le TMS calcule l'accrual en appliquant un taux moyen journalier à la
    valeur nominale. Morpho, lui, compose les intérêts à chaque bloc.
    L'écart est dû à l'arrondi de la composition continue vs le taux moyen.
    Un écart de cet ordre est ACCEPTABLE et doit être toléré dans la procédure.

    ÉCART SIGNIFICATIF (> 0.10%) :
    Peut indiquer que le TMS utilise un taux incorrect (par exemple un taux
    figé à l'ouverture de position et non mis à jour quotidiennement).

    Args:
        wallet         : adresse du wallet institutionnel
        market_id_hex  : ID du marché (bytes32 hex)
        interets_tms   : intérêts calculés par le TMS sur la période (en tokens)
        loan_symbol    : symbole du loan asset
        bloc_reference : bloc de référence EOD t₀ (si None, utilise ~24h ago)
    """
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI
    )

    wallet_cs = Web3.to_checksum_address(wallet)
    market_id_bytes = bytes.fromhex(market_id_hex[2:])

    try:
        # Bloc actuel = t₁
        bloc_t1 = w3.eth.block_number

        # Bloc t₀ : soit fourni, soit ~24h ago
        if bloc_reference is None:
            BLOCS_PAR_JOUR = 7200  # ~24h
            bloc_t0 = max(0, bloc_t1 - BLOCS_PAR_JOUR)
        else:
            bloc_t0 = bloc_reference

        # ── Lecture des états on-chain à t₀ et t₁ ─────────────────────────────
        mkt_t0  = morpho.functions.market(market_id_bytes).call(
            block_identifier=bloc_t0
        )
        mkt_t1  = morpho.functions.market(market_id_bytes).call(
            block_identifier=bloc_t1
        )
        pos_t0  = morpho.functions.position(market_id_bytes, wallet_cs).call(
            block_identifier=bloc_t0
        )
        pos_t1  = morpho.functions.position(market_id_bytes, wallet_cs).call(
            block_identifier=bloc_t1
        )

        # ── Accrual on-chain : Assets(t₁) - Assets(t₀) ────────────────────────
        supply_t0 = shares_to_assets(pos_t0[0], mkt_t0[0], mkt_t0[1])
        supply_t1 = shares_to_assets(pos_t1[0], mkt_t1[0], mkt_t1[1])

        loan_info      = TOKEN_INFO.get(loan_symbol,
                                        {"decimals": 18})
        loan_dec       = loan_info.get("decimals", 18) if isinstance(
                            loan_info, dict) else 18

        # Chercher les décimales dans TOKEN_INFO par symbole
        for addr, info in TOKEN_INFO.items():
            if info["symbol"] == loan_symbol:
                loan_dec = info["decimals"]
                break

        interets_onchain = (supply_t1 - supply_t0) / (10**loan_dec)

        # ── Écart accrual TMS vs on-chain ─────────────────────────────────────
        ecart_abs = interets_onchain - interets_tms
        ecart_pct = (abs(ecart_abs) / interets_onchain * 100
                     if interets_onchain > 0 else 0.0)

        statut = classifier_ecart(ecart_pct, abs(ecart_abs), loan_symbol)

        # ── Note explicative selon l'écart ────────────────────────────────────
        if statut == StatutEcart.OK:
            note = ("Écart dans la tolérance normale — arrondi de conversion "
                    "décimale entre le calcul TMS et l'accrual per-block Morpho.")
        elif statut == StatutEcart.ACCEPTABLE:
            note = ("Micro-écart d'accrual — résulte de la composition continue "
                    "on-chain vs le taux moyen journalier utilisé par le TMS. "
                    "Acceptable selon la procédure de réconciliation (< 0.10%).")
        elif statut == StatutEcart.INVESTIGATION:
            note = ("Écart significatif — vérifier que le TMS utilise bien le "
                    "taux Morpho du jour J-1 (pas un taux figé à l'ouverture). "
                    "Vérifier qu'aucune transaction n'a été manquée.")
        else:
            note = ("CRITIQUE — écart > 0.50% ou montant absolu significatif. "
                    "Probabilité d'erreur de booking ou de transaction non "
                    "intégrée dans le TMS. Vérification manuelle requise.")

        return {
            "market_id":         market_id_hex,
            "wallet":            wallet,
            "bloc_t0":           bloc_t0,
            "bloc_t1":           bloc_t1,
            "supply_assets_t0":  supply_t0 / (10**loan_dec),
            "supply_assets_t1":  supply_t1 / (10**loan_dec),
            "interets_onchain":  round(interets_onchain, 8),
            "interets_tms":      round(interets_tms, 8),
            "ecart_absolu":      round(ecart_abs, 8),
            "ecart_pct":         round(ecart_pct, 6),
            "statut":            statut.value,
            "note":              note,
            "devise":            loan_symbol,
        }

    except Exception as e:
        return {
            "market_id": market_id_hex,
            "erreur":    str(e),
            "statut":    StatutEcart.CRITIQUE.value,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# RAPPORT DE RÉCONCILIATION CONSOLIDÉ
# ═══════════════════════════════════════════════════════════════════════════════

def generer_rapport_reconciliation(
    wallet: str,
    positions_tms: list[dict],
    interets_tms_par_marche: dict | None = None,
) -> dict:
    """
    Génère le rapport de réconciliation complet pour un wallet institutionnel.

    Combine la réconciliation Niveau 1 (positions nominales) et Niveau 2
    (intérêts accumulés) en un rapport structuré exportable en JSON et CSV.

    Args:
        wallet                   : adresse du wallet institutionnel
        positions_tms            : positions extraites du TMS (liste de dicts)
        interets_tms_par_marche  : dict {market_id: montant_interets_tms}

    Returns:
        Rapport de réconciliation complet
    """
    ts = datetime.utcnow()

    print(f"\n{'═'*62}")
    print(f"RÉCONCILIATION — {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Wallet : {wallet[:20]}...{wallet[-6:]}")
    print(f"{'═'*62}")

    # ── Extraction des market IDs depuis les positions TMS ─────────────────
    market_ids = list({p["market_id"] for p in positions_tms})

    # ── Lecture des positions on-chain ─────────────────────────────────────
    print(f"\n  Lecture on-chain de {len(market_ids)} marché(s)...")
    positions_oc = lire_positions_onchain(wallet, market_ids)

    # ── Niveau 1 : Réconciliation des positions nominales ──────────────────
    print("  Réconciliation Niveau 1 : positions nominales...")
    recos_n1 = reconcilier_positions_nominales(positions_oc, positions_tms)

    # ── Niveau 2 : Réconciliation des intérêts ─────────────────────────────
    recos_n2 = []
    if interets_tms_par_marche:
        print("  Réconciliation Niveau 2 : intérêts accumulés...")
        for market_id, interets_tms in interets_tms_par_marche.items():
            loan_symbol = next(
                (r["supply"]["devise"] for r in recos_n1
                 if r["market_id"] == market_id), "USDC"
            )
            reco = reconcilier_interets(
                wallet, market_id, interets_tms, loan_symbol
            )
            recos_n2.append(reco)

    # ── Synthèse ───────────────────────────────────────────────────────────
    tous_statuts = (
        [r["statut_global"] for r in recos_n1] +
        [r.get("statut", "") for r in recos_n2]
    )

    n_critique     = sum(1 for s in tous_statuts if "CRITIQUE"    in s)
    n_investigation= sum(1 for s in tous_statuts if "INVESTIGATION" in s)
    n_acceptable   = sum(1 for s in tous_statuts if "ACCEPTABLE"  in s)
    n_ok           = sum(1 for s in tous_statuts if s == "✅ OK")

    statut_global = (
        "🚨 CRITIQUE" if n_critique > 0 else
        "⚠️  INVESTIGATION" if n_investigation > 0 else
        "ℹ️  ACCEPTABLE" if n_acceptable > 0 else
        "✅ OK"
    )

    # ── Affichage ──────────────────────────────────────────────────────────
    print(f"\n{'─'*62}")
    print(f"RÉSULTATS — NIVEAU 1 : POSITIONS NOMINALES")
    print(f"{'─'*62}")
    print(f"  {'Marché':<28} {'Supply':>14} {'Borrow':>14} {'Collat.':>12}  {'Statut'}")
    print(f"  {'─'*80}")

    for r in recos_n1:
        if "erreur" in r:
            print(f"  {r['market_id'][:26]:<28} ❌ ERREUR")
            continue

        sup_ecart  = f"{r['supply']['ecart_pct']:.3f}%"
        bor_ecart  = f"{r['borrow']['ecart_pct']:.3f}%"
        col_ecart  = f"{r['collateral']['ecart_pct']:.3f}%"
        marche     = r['marche'][:28]

        print(f"  {marche:<28} {sup_ecart:>14} {bor_ecart:>14} {col_ecart:>12}  {r['statut_global']}")

        # Détail des écarts non-OK
        for dim in ["supply", "borrow", "collateral"]:
            d = r[dim]
            if "OK" not in d["statut"] and d["onchain"] > 0:
                print(f"    ↳ {dim.upper()}: "
                      f"on-chain={d['onchain']:,.4f} vs TMS={d['tms']:,.4f} "
                      f"{d['devise']} | écart={d['ecart_absolu']:+.6f} "
                      f"({d['sens']})")

    if recos_n2:
        print(f"\n{'─'*62}")
        print(f"RÉSULTATS — NIVEAU 2 : INTÉRÊTS ACCUMULÉS")
        print(f"{'─'*62}")
        for r in recos_n2:
            if "erreur" in r:
                continue
            print(f"\n  Marché : {r['market_id'][:20]}...")
            print(f"  Intérêts on-chain : {r['interets_onchain']:>14,.8f} {r['devise']}")
            print(f"  Intérêts TMS      : {r['interets_tms']:>14,.8f} {r['devise']}")
            print(f"  Écart             : {r['ecart_absolu']:>+14,.8f} ({r['ecart_pct']:.4f}%)")
            print(f"  Statut            : {r['statut']}")
            print(f"  Note              : {r['note']}")

    print(f"\n{'═'*62}")
    print(f"STATUT GLOBAL : {statut_global}")
    print(f"  🚨 Critiques      : {n_critique}")
    print(f"  ⚠️  Investigations : {n_investigation}")
    print(f"  ℹ️  Acceptables    : {n_acceptable}")
    print(f"  ✅ OK             : {n_ok}")

    rapport = {
        "metadata": {
            "timestamp":      ts.isoformat(),
            "bloc":           w3.eth.block_number,
            "wallet":         wallet,
            "nb_marches":     len(market_ids),
            "statut_global":  statut_global,
            "nb_critique":    n_critique,
            "nb_investigation": n_investigation,
            "nb_acceptable":  n_acceptable,
            "nb_ok":          n_ok,
        },
        "niveau_1_positions": recos_n1,
        "niveau_2_interets":  recos_n2,
    }

    return rapport


def exporter_csv(rapport: dict, filename: str = "reconciliation.csv") -> None:
    """
    Exporte le rapport de réconciliation en format CSV plat,
    compatible avec Excel et les outils de reporting back-office.
    """
    rows = []
    ts = rapport["metadata"]["timestamp"]

    for r in rapport["niveau_1_positions"]:
        if "erreur" in r:
            continue
        for dim, label in [("supply", "Supply"), ("borrow", "Borrow"),
                            ("collateral", "Collateral")]:
            d = r[dim]
            if d["onchain"] > 0 or d["tms"] > 0:
                rows.append({
                    "timestamp":        ts,
                    "market_id":        r["market_id"],
                    "marche":           r["marche"],
                    "type_position":    label,
                    "devise":           d["devise"],
                    "valeur_onchain":   d["onchain"],
                    "valeur_tms":       d["tms"],
                    "ecart_absolu":     d["ecart_absolu"],
                    "ecart_pct":        d["ecart_pct"],
                    "sens_ecart":       d["sens"],
                    "statut":           d["statut"],
                    "utilisation_pct":  r.get("utilisation_pct", ""),
                })

    for r in rapport.get("niveau_2_interets", []):
        if "erreur" in r:
            continue
        rows.append({
            "timestamp":        ts,
            "market_id":        r["market_id"],
            "marche":           "",
            "type_position":    "Intérêts accrual",
            "devise":           r.get("devise", ""),
            "valeur_onchain":   r.get("interets_onchain", 0),
            "valeur_tms":       r.get("interets_tms", 0),
            "ecart_absolu":     r.get("ecart_absolu", 0),
            "ecart_pct":        r.get("ecart_pct", 0),
            "sens_ecart":       "",
            "statut":           r.get("statut", ""),
            "utilisation_pct":  "",
        })

    if not rows:
        return

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n  📊 Export CSV : {filename} ({len(rows)} lignes)")


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("SCRIPT 8 — RÉCONCILIATION POSITIONS MORPHO BLUE")
    print("On-chain (source de vérité) vs Systèmes internes (OMS/TMS)")
    print("Section IV.4 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*62)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")

    WALLET = os.getenv(
        "WALLET_INSTITUTION",
        "0x4e9d257FfEce3C9fAb9D8D5e4e6e14C98E6b6b6b"
    )

    # ── Positions TMS simulées ─────────────────────────────────────────────────
    # En production : extraire depuis la base de données du TMS via API
    # Ici : positions fictives avec micro-écarts intentionnels pour démonstration
    POSITIONS_TMS_SIMUL = [
        {
            "market_id":    "0xb323495f7e4148be5643a4ea4a8221eef163e4bccfdedc2a6f4696baacbc86cc",
            "supply_assets":  5_000_000.00,    # USDC — légèrement sous-estimé
            "borrow_assets":  0.0,
            "collateral":     0.0,
        },
        {
            "market_id":    "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49",
            "supply_assets":  3_000_000.00,    # USDC
            "borrow_assets":  0.0,
            "collateral":     0.0,
        },
        {
            "market_id":    "0x7dde86a1e94561d9690ec678db673c1a6396365f7d1d65e129c5fff0990ff758",
            "supply_assets":  0.0,
            "borrow_assets":  1_800_000.00,    # USDC emprunté
            "collateral":     800.0,           # WETH déposé comme collatéral
        },
    ]

    # Intérêts TMS simulés (accrual EOD calculé par le TMS)
    INTERETS_TMS_SIMUL = {
        "0xb323495f7e4148be5643a4ea4a8221eef163e4bccfdedc2a6f4696baacbc86cc": 825.12,  # USDC
        "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49": 510.44,  # USDC
    }

    # ── Génération du rapport ─────────────────────────────────────────────────
    rapport = generer_rapport_reconciliation(
        wallet=WALLET,
        positions_tms=POSITIONS_TMS_SIMUL,
        interets_tms_par_marche=INTERETS_TMS_SIMUL,
    )

    # ── Export JSON ────────────────────────────────────────────────────────────
    json_file = "morpho_reconciliation_report.json"
    with open(json_file, "w") as f:
        json.dump(rapport, f, indent=2, default=str)

    # ── Export CSV ─────────────────────────────────────────────────────────────
    exporter_csv(rapport, "morpho_reconciliation_report.csv")

    print(f"\n{'━'*62}")
    print(f"💾 Rapport JSON : {json_file}")
    print(f"\nPROCÉDURE DE RÉCONCILIATION QUOTIDIENNE :")
    for etape, desc in [
        ("1. Extraire", "Les positions du TMS via API interne"),
        ("2. Lire",     "Les positions on-chain via ce script (Source de vérité)"),
        ("3. Comparer", "Via reconcilier_positions_nominales()"),
        ("4. Accrual",  "Via reconcilier_interets() pour les positions supply"),
        ("5. Classifier","Selon les seuils : OK / ACCEPTABLE / INVESTIGATION / CRITIQUE"),
        ("6. Exporter",  "JSON + CSV pour l'équipe back-office"),
        ("7. Escalader", "Les écarts CRITIQUE vers le middle-office dans la journée"),
    ]:
        print(f"  {etape:<12} {desc}")
    print("━"*62)