"""
SCRIPT 10 — Reporting réglementaire EMIR/SFTR
           depuis les événements on-chain Morpho Blue

Contexte : EMIR REFIT (applicable depuis le 29 avril 2024) impose la
           déclaration des dérivés financiers aux trade repositories.
           Ce script génère les fichiers de reporting à partir des
           événements on-chain Morpho Blue, dans un format compatible
           avec les systèmes de reporting existants.

           QUALIFICATION RÉGLEMENTAIRE (cf. Section IV.5 du mémoire) :

           EMIR :
           - Supply/Borrow simples → probablement HORS SCOPE EMIR
             (s'apparentent à un prêt ou dépôt de trésorerie, pas à un dérivé)
           - Morpho V2 (taux fixe / terme fixe) → potentiellement dans le SCOPE
             (peut être qualifié comme contrat de prêt à terme, voire dérivé de taux)
           - Si dans le scope : champ 2.12 "Derivative based on crypto-assets"
             à renseigner (introduit par EMIR REFIT)

           SFTR :
           - Securities Financing Transactions Regulation — applicable si les
             actifs utilisés sont des "securities" (titres financiers)
           - USDC, wstETH → qualification variable selon les juridictions
           - À valider avec l'équipe juridique avant toute déclaration

           MiCA :
           - Morpho Blue = protocole entièrement décentralisé → probablement
             exempté en tant que service DeFi sans entité opératrice identifiable
           - USDC, EURCV = e-money tokens (EMT) → soumis à MiCA
           - À vérifier que l'émetteur détient l'agrément MiCA requis

           ⚠️ Ce script génère les données dans un format de reporting structuré
           mais NE constitue PAS un avis juridique sur la qualification EMIR/SFTR.
           Un avis juridique spécialisé est indispensable avant toute déclaration.

Dépendances :
    pip install web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>
    LEI_INSTITUTION=<LEI de l'institution (20 caractères)>
    BIC_INSTITUTION=<BIC de l'institution>

Sources :
  - ESMA, EMIR Reporting — RTS under EMIR REFIT, applicable depuis le 29/04/2024
    https://www.esma.europa.eu/data-reporting/emir-reporting
  - Novatus Global, Is Crypto Trading Reportable Under EMIR & MiFIR?, 2025
    https://novatus.global/is-crypto-trading-reportable-under-emir-and-mifir/
  - Calibraint, DeFi Regulatory Compliance 2025
    https://www.calibraint.com/blog/defi-regulatory-compliance-sec-cftc-2025
"""

import os
import csv
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.dom.minidom import parseString
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── CONFIGURATION INSTITUTION ────────────────────────────────────────────────

# En production : renseigner dans .env
LEI_INSTITUTION = os.getenv("LEI_INSTITUTION", "XXXXXXXXXXXXXXXXXX00")
BIC_INSTITUTION = os.getenv("BIC_INSTITUTION", "XXXXXXXXX")
NOM_INSTITUTION = os.getenv("NOM_INSTITUTION", "Institution Financière SA")
PAYS_INSTITUTION = os.getenv("PAYS_INSTITUTION", "FR")

# Qualification de l'institution selon EMIR
# FC = Financial Counterparty (banque, gestionnaire d'actifs)
# NFC+ = Non-Financial Counterparty above threshold
NATURE_CONTREPARTIE = "FC"  # À adapter selon la qualification de l'institution
SECTEUR_CORPORATIF  = "A"   # A = Financier (selon classification NACE EMIR)

# ─── ADRESSES ─────────────────────────────────────────────────────────────────

MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

TOKEN_INFO = {
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": {
        "symbol": "USDC", "decimals": 6,
        "mica_type": "EMT",  # e-money token
        "mica_issuer": "Circle Internet Financial",
        "isin": None,
    },
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": {
        "symbol": "USDT", "decimals": 6,
        "mica_type": "EMT",
        "mica_issuer": "Tether Operations Limited",
        "isin": None,
    },
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": {
        "symbol": "WETH", "decimals": 18,
        "mica_type": "CRYPTO",
        "mica_issuer": None,
        "isin": None,
    },
    "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0": {
        "symbol": "wstETH", "decimals": 18,
        "mica_type": "CRYPTO",
        "mica_issuer": "Lido Finance",
        "isin": None,
    },
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {
        "symbol": "WBTC", "decimals": 8,
        "mica_type": "CRYPTO",
        "mica_issuer": None,
        "isin": None,
    },
    "0x6B175474E89094C44Da98b954EedeAC495271d0F": {
        "symbol": "DAI", "decimals": 18,
        "mica_type": "ART",  # asset-referenced token
        "mica_issuer": "MakerDAO",
        "isin": None,
    },
}

# ─── ABIs ─────────────────────────────────────────────────────────────────────

MORPHO_EVENTS_ABI = [
    {
        "name": "Supply",
        "type": "event",
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
        "name": "Withdraw",
        "type": "event",
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
        "inputs": [
            {"name": "id",       "type": "bytes32", "indexed": True},
            {"name": "caller",   "type": "address", "indexed": False},
            {"name": "onBehalf", "type": "address", "indexed": True},
            {"name": "assets",   "type": "uint256", "indexed": False},
            {"name": "shares",   "type": "uint256", "indexed": False},
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


# ═══════════════════════════════════════════════════════════════════════════════
# QUALIFICATION RÉGLEMENTAIRE
# ═══════════════════════════════════════════════════════════════════════════════

def qualifier_operation_emir(event_type: str, est_taux_fixe: bool = False) -> dict:
    """
    Détermine la qualification EMIR d'une opération Morpho Blue.

    Logique de qualification (cf. Section IV.5 du mémoire) :

    EMIR couvre les "contrats dérivés" au sens de l'annexe I section C
    de MiFID II. Les opérations de prêt/emprunt collateralisé ne sont
    généralement pas des dérivés au sens EMIR, SAUF si elles présentent
    des caractéristiques contractuelles de dérivés (taux fixe + terme fixe
    → potentiellement un IRS ou un FRA crypto).

    SFTR couvre les opérations de "securities financing" (repo, prêt de
    titres, buy-sell back). S'applique si les actifs concernés sont des
    "securities" au sens de la directive MiFID II.

    Args:
        event_type   : type d'opération Morpho (SUPPLY, BORROW, etc.)
        est_taux_fixe: True si l'opération est sur un marché Morpho V2
                       à taux fixe et terme fixe

    Returns:
        Dictionnaire de qualification réglementaire
    """
    if est_taux_fixe:
        # Morpho V2 : taux fixe + terme fixe → potentiellement dans le scope EMIR
        emir_scope = "POTENTIELLEMENT_IN_SCOPE"
        emir_note  = ("Marché Morpho V2 à taux fixe et terme fixe — "
                      "potentiellement qualifiable comme contrat de taux "
                      "ou prêt à terme selon l'interprétation EMIR. "
                      "Avis juridique requis.")
        champ_212  = "TRUE"  # Champ EMIR REFIT 2.12 : dérivé sur crypto-actifs
    else:
        # Morpho Blue standard : taux variable, pas de terme fixe
        emir_scope = "PROBABLEMENT_HORS_SCOPE"
        emir_note  = ("Supply/Borrow Morpho Blue à taux variable sans terme fixe "
                      "— s'apparente à un prêt ou dépôt de trésorerie, "
                      "probablement hors scope EMIR. Avis juridique requis.")
        champ_212  = "NOT_APPLICABLE"

    # SFTR : applicable si les actifs sont des "securities"
    sftr_note = ("USDC, USDT et autres stablecoins ne sont généralement pas "
                 "qualifiés de 'securities' au sens MiFID II dans la plupart "
                 "des juridictions européennes. SFTR probablement hors scope. "
                 "À confirmer avec l'équipe juridique.")

    # MiCA : Morpho Blue = protocole décentralisé
    mica_note = ("Morpho Blue est un protocole entièrement décentralisé sans "
                 "entité opératrice identifiable — probablement exempté de MiCA "
                 "en tant que service DeFi. Les stablecoins utilisés (USDC, USDT) "
                 "sont des EMT soumis à MiCA — vérifier l'agrément de l'émetteur.")

    return {
        "emir_scope":    emir_scope,
        "emir_note":     emir_note,
        "champ_2_12":    champ_212,
        "sftr_scope":    "PROBABLEMENT_HORS_SCOPE",
        "sftr_note":     sftr_note,
        "mica_scope":    "PROTOCOLE_EXEMPTE",
        "mica_note":     mica_note,
        "avis_requis":   True,
        "avis_note":     ("⚠️  Ce script génère les données dans un format de "
                          "reporting structuré mais NE constitue PAS un avis "
                          "juridique. Un avis juridique spécialisé est "
                          "indispensable avant toute déclaration EMIR/SFTR."),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION DES ÉVÉNEMENTS ON-CHAIN
# ═══════════════════════════════════════════════════════════════════════════════

def extraire_evenements(
    wallet: str,
    nb_blocs: int = 7200,
    types: list | None = None,
) -> list[dict]:
    """
    Extrait les événements Morpho Blue d'un wallet sur les derniers N blocs.

    Args:
        wallet   : adresse du wallet institutionnel
        nb_blocs : nombre de blocs à parcourir (~24h = 7200 blocs)
        types    : liste des types d'événements à extraire

    Returns:
        Liste des événements bruts normalisés
    """
    if types is None:
        types = ["Supply", "Borrow", "Withdraw", "Repay"]

    morpho    = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_EVENTS_ABI + MORPHO_PARAMS_ABI
    )
    wallet_cs = Web3.to_checksum_address(wallet)
    bloc_fin  = w3.eth.block_number
    bloc_deb  = max(0, bloc_fin - nb_blocs)

    print(f"\n  Extraction des événements : blocs #{bloc_deb:,} → #{bloc_fin:,}")

    evenements = []
    market_cache = {}

    for event_name in types:
        try:
            f = morpho.events[event_name].create_filter(
                from_block=bloc_deb,
                to_block=bloc_fin,
                argument_filters={"onBehalf": wallet_cs}
            )
            evts = f.get_all_entries()

        except Exception:
            try:
                f = morpho.events[event_name].create_filter(
                    from_block=bloc_deb, to_block=bloc_fin
                )
                evts = [
                    e for e in f.get_all_entries()
                    if e["args"].get("onBehalf", "").lower() == wallet.lower()
                ]
            except Exception as e2:
                print(f"  ⚠️  Impossible de récupérer {event_name} : {e2}")
                continue

        for evt in evts:
            tx_hash     = evt["transactionHash"].hex()
            market_id   = "0x" + evt["args"]["id"].hex()
            block_data  = w3.eth.get_block(evt["blockNumber"])
            tx_receipt  = w3.eth.get_transaction_receipt(tx_hash)
            ts          = datetime.fromtimestamp(
                block_data["timestamp"], tz=timezone.utc
            )

            # Paramètres immuables du marché (avec cache)
            if market_id not in market_cache:
                try:
                    morpho_p = w3.eth.contract(
                        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
                        abi=MORPHO_PARAMS_ABI
                    )
                    p = morpho_p.functions.idToMarketParams(
                        evt["args"]["id"]
                    ).call()
                    loan_info = TOKEN_INFO.get(
                        Web3.to_checksum_address(p[0]),
                        {"symbol": "UNKNOWN", "decimals": 18, "mica_type": "CRYPTO", "mica_issuer": None, "isin": None}
                    )
                    coll_info = TOKEN_INFO.get(
                        Web3.to_checksum_address(p[1]),
                        {"symbol": "UNKNOWN", "decimals": 18, "mica_type": "CRYPTO", "mica_issuer": None, "isin": None}
                    )
                    market_cache[market_id] = {
                        "loan_token":  p[0], "loan_symbol":  loan_info["symbol"],
                        "loan_dec":    loan_info["decimals"],
                        "loan_mica":   loan_info.get("mica_type"),
                        "coll_token":  p[1], "coll_symbol":  coll_info["symbol"],
                        "coll_dec":    coll_info["decimals"],
                        "coll_mica":   coll_info.get("mica_type"),
                        "lltv":        p[4] / 1e18,
                    }
                except Exception:
                    market_cache[market_id] = None

            mp = market_cache[market_id]

            # Montant et gas
            dec = mp["loan_dec"] if mp else 18
            montant = evt["args"].get("assets", 0) / (10**dec)
            gas_eth = (tx_receipt["gasUsed"] *
                       tx_receipt.get("effectiveGasPrice", 0)) / 1e18

            evenements.append({
                "event_type":  event_name.upper(),
                "tx_hash":     tx_hash,
                "bloc":        evt["blockNumber"],
                "timestamp":   ts,
                "market_id":   market_id,
                "market_params": mp,
                "on_behalf":   evt["args"].get("onBehalf", ""),
                "caller":      evt["args"].get("caller", ""),
                "montant":     montant,
                "shares":      evt["args"].get("shares", 0) / 1e18,
                "gas_eth":     gas_eth,
            })

    # Trier par bloc croissant
    evenements.sort(key=lambda x: x["bloc"])
    print(f"  {len(evenements)} événement(s) extrait(s)")
    return evenements


# ═══════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION DU RAPPORT EMIR (CSV)
# ═══════════════════════════════════════════════════════════════════════════════

def generer_rapport_emir_csv(
    evenements: list[dict],
    output_file: str = "emir_report.csv",
) -> list[dict]:
    """
    Génère le rapport EMIR au format CSV selon les champs EMIR REFIT.

    Les champs sont organisés selon les tableaux de l'Annexe I du RTS
    EMIR REFIT (Règlement délégué UE 2022/1855), en particulier :
    - Table 1 : Données sur les contreparties
    - Table 2 : Données sur le contrat
    - Champ 2.12 : "Derivative based on crypto-assets" (nouveau EMIR REFIT)

    CHAMPS RENSEIGNÉS / NON RENSEIGNÉS :
    Certains champs EMIR standards ne peuvent pas être renseignés pour
    les opérations Morpho (ex. LEI contrepartie, ISIN) en l'absence
    d'une qualification juridique formelle. Ces champs sont marqués
    "NON_APPLICABLE_DEFI" pour indiquer qu'ils requièrent une décision
    de l'équipe juridique.

    Args:
        evenements  : liste des événements extraits
        output_file : nom du fichier CSV de sortie

    Returns:
        Liste des enregistrements EMIR générés
    """
    records = []

    for evt in evenements:
        mp = evt.get("market_params")
        ts = evt["timestamp"]
        qualif = qualifier_operation_emir(evt["event_type"])

        # Génération UTI (format interne — à adapter pour conformité EMIR)
        # Format EMIR officiel : {LEI_déclarant}{date}{suffixe_unique}
        # Format interne : MORPHO-{TYPE}-{HASH}
        tx_short = evt["tx_hash"][2:10].upper()
        uti_interne = f"MORPHO-{evt['event_type'][:3]}-{tx_short}"
        uti_emir    = f"{LEI_INSTITUTION}{ts.strftime('%Y%m%d')}{tx_short}"

        loan_symbol = mp["loan_symbol"] if mp else "UNKNOWN"
        coll_symbol = mp["coll_symbol"] if mp else "UNKNOWN"
        loan_mica   = mp.get("loan_mica", "UNKNOWN") if mp else "UNKNOWN"

        record = {

            # ── TABLE 1 — DONNÉES SUR LES CONTREPARTIES ─────────────────────

            # 1.1 - LEI de la contrepartie déclarante (institution)
            "1.1_LEI_declarant":            LEI_INSTITUTION,

            # 1.2 - Nature de la contrepartie déclarante
            "1.2_nature_declarant":         NATURE_CONTREPARTIE,

            # 1.3 - Secteur corporatif
            "1.3_secteur_corporatif":       SECTEUR_CORPORATIF,

            # 1.4 - Nature de l'autre contrepartie
            # Morpho Blue = smart contract → pas de nature EMIR standard
            "1.4_nature_autre_contrepartie": "NON_APPLICABLE_DEFI",

            # 1.5 - LEI de l'autre contrepartie
            # Morpho Blue n'a pas de LEI → convention interne
            "1.5_LEI_autre_contrepartie":   "MORPHO_BLUE_ETH_0xBBBB",
            "1.5_note":                     ("Pas de LEI officiel — smart contract "
                                             "sans entité juridique identifiable."),

            # 1.6 - Pays de l'autre contrepartie
            "1.6_pays_autre_contrepartie":  "NON_APPLICABLE_DEFI",

            # ── TABLE 2 — DONNÉES SUR LE CONTRAT ─────────────────────────────

            # 2.1 - Unique Trade Identifier (UTI)
            # Format EMIR officiel = LEI + date + suffixe
            # Format interne = MORPHO-TYPE-HASH (à convertir en production)
            "2.1_uti":                      uti_emir,
            "2.1_uti_interne":              uti_interne,
            "2.1_uti_note":                 ("UTI généré depuis le tx hash. "
                                             "Format à valider avec le trade "
                                             "repository avant déclaration."),

            # 2.2 - Timestamp de reporting
            "2.2_timestamp_reporting":      datetime.utcnow().isoformat(),

            # 2.3 - Lieu d'exécution (MIC ou "XOFF" pour OTC)
            # Morpho Blue = protocole DeFi → "XOFF" (OTC) ou "XXXX"
            "2.3_lieu_execution":           "XOFF",
            "2.3_note":                     ("Protocole DeFi = OTC non standardisé "
                                             "— utilisation de XOFF par défaut."),

            # 2.4 - Compression
            "2.4_compression":              "false",

            # 2.5 - Prix / taux
            # Sur Morpho : taux variable algorithmique — pas de taux contractuel fixe
            "2.5_prix_taux":                "VARIABLE_ALGORITHMIQUE",
            "2.5_note":                     ("Taux Morpho Adaptive Curve IRM — "
                                             "varie à chaque bloc. Pas de taux "
                                             "contractuel fixe à déclarer."),

            # 2.6 - Devise du notionnel
            "2.6_devise_notionnel":         loan_symbol,

            # 2.7 - Montant notionnel
            "2.7_montant_notionnel":        round(evt["montant"], 6),

            # 2.8 - Devise de règlement
            "2.8_devise_reglement":         loan_symbol,

            # 2.9 - Date d'échéance
            # Morpho Blue standard : pas de terme fixe
            "2.9_date_echeance":            ("NON_APPLICABLE_TAUX_VARIABLE"
                                             if evt["event_type"] in ("SUPPLY", "BORROW")
                                             else ""),

            # 2.10 - Type de contrat
            # Supply/Borrow = prêt collateralisé → probablement hors scope dérivés
            "2.10_type_contrat":            ("PRET_COLLATÉRALISE_DEFI"
                                             if evt["event_type"] in ("SUPPLY", "BORROW")
                                             else "REMBOURSEMENT_DEFI"),

            # 2.11 - Classe d'actifs du sous-jacent
            "2.11_classe_actifs":           "CR",  # CR = Crypto (selon classification EMIR REFIT)

            # 2.12 - NOUVEAU EMIR REFIT : Dérivé basé sur des crypto-actifs
            # Ce champ est l'un des ajouts majeurs d'EMIR REFIT pour les crypto
            "2.12_crypto_based":            qualif["champ_2_12"],
            "2.12_note":                    ("Champ 2.12 EMIR REFIT — 'Derivative "
                                             "based on crypto-assets'. Renseigner "
                                             "TRUE si l'opération est qualifiée "
                                             "comme dérivé sur crypto-actifs."),

            # 2.13 - Identifiant de l'actif sous-jacent
            # USDC / wstETH n'ont pas d'ISIN → adresse ERC-20
            "2.13_id_sous_jacent":          mp["loan_token"] if mp else "",
            "2.13_type_id":                 "CRYPTO_ADDRESS",
            "2.13_note":                    ("Pas d'ISIN officiel pour les crypto-actifs "
                                             "— utilisation de l'adresse ERC-20."),

            # ── DONNÉES SUPPLÉMENTAIRES (hors champs EMIR standard) ────────────

            # Référence de règlement on-chain (tx hash = équivalent MT54x)
            "ref_reglement":                evt["tx_hash"],
            "bloc":                         evt["bloc"],
            "timestamp_operation":          ts.isoformat(),
            "marche_morpho":                f"{loan_symbol}/{coll_symbol} "
                                            f"(LLTV {mp['lltv']*100:.1f}%)" if mp else "",

            # Classification MiCA de l'actif de prêt
            "mica_classification_loan":     loan_mica,
            "mica_note":                    qualif["mica_note"][:100],

            # Gas fees (coût opérationnel de règlement)
            "gas_fees_eth":                 round(evt["gas_eth"], 8),

            # Statut de qualification
            "emir_scope":                   qualif["emir_scope"],
            "sftr_scope":                   qualif["sftr_scope"],
            "avis_juridique_requis":        "OUI",
        }

        records.append(record)

    # Export CSV
    if records:
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
        print(f"  💾 Rapport EMIR CSV : {output_file} ({len(records)} lignes)")
    else:
        print("  ℹ️  Aucun événement à déclarer — fichier CSV non généré")

    return records


# ═══════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION DU RAPPORT XML ISO 20022
# ═══════════════════════════════════════════════════════════════════════════════

def generer_rapport_xml(
    records: list[dict],
    output_file: str = "emir_report.xml",
) -> str:
    """
    Génère le rapport EMIR au format XML ISO 20022.

    Utilise le message DerivTx (Derivative Transaction Report) de l'ISO 20022,
    qui est le format cible des trade repositories européens (DTCC, Regis-TR,
    UnaVista) conformément aux exigences EMIR REFIT.

    Args:
        records     : liste des enregistrements EMIR générés par
                      generer_rapport_emir_csv()
        output_file : nom du fichier XML de sortie
    """
    root = ET.Element("Document")
    root.set("xmlns", "urn:iso:std:iso:20022:tech:xsd:auth.030.001.03")

    rpt = ET.SubElement(root, "DerivsTradRpt")

    # ── Header ─────────────────────────────────────────────────────────────────
    hdr = ET.SubElement(rpt, "RptHdr")
    ET.SubElement(hdr, "NbRcrds").text = str(len(records))
    ET.SubElement(hdr, "RptgDtTm").text = datetime.utcnow().isoformat()
    ET.SubElement(hdr, "RptgCtrPty").text = LEI_INSTITUTION

    # ── Avertissement réglementaire ────────────────────────────────────────────
    warn = ET.SubElement(rpt, "RgltryWrnng")
    ET.SubElement(warn, "Msg").text = (
        "AVERTISSEMENT : Ce fichier XML est généré à titre informatif. "
        "La qualification EMIR des opérations Morpho Blue n'est pas "
        "formellement établie. Un avis juridique spécialisé est requis "
        "avant toute soumission à un trade repository."
    )

    # ── Enregistrements ────────────────────────────────────────────────────────
    for rec in records:
        tx_rpt = ET.SubElement(rpt, "TxRpt")

        # Contreparties
        ctrptys = ET.SubElement(tx_rpt, "CtrPtySpcfcData")
        rptg    = ET.SubElement(ctrptys, "RptgCtrPty")
        ET.SubElement(rptg, "LEI").text  = rec["1.1_LEI_declarant"]
        ET.SubElement(rptg, "Ntr").text  = rec["1.2_nature_declarant"]
        ET.SubElement(rptg, "Sctr").text = rec["1.3_secteur_corporatif"]

        other = ET.SubElement(ctrptys, "OthrCtrPty")
        ET.SubElement(other, "Ref").text   = rec["1.5_LEI_autre_contrepartie"]
        ET.SubElement(other, "Note").text  = rec["1.5_note"][:80]

        # Données du contrat
        cmon = ET.SubElement(tx_rpt, "CmonData")
        ET.SubElement(cmon, "TxId").text    = rec["2.1_uti"]
        ET.SubElement(cmon, "TxIdIntl").text = rec["2.1_uti_interne"]
        ET.SubElement(cmon, "RptTmStmp").text = rec["2.2_timestamp_reporting"]
        ET.SubElement(cmon, "ExctnVn").text = rec["2.3_lieu_execution"]

        # Détails du contrat
        ctrct = ET.SubElement(tx_rpt, "CtrctData")
        ET.SubElement(ctrct, "CtrctTp").text     = rec["2.10_type_contrat"]
        ET.SubElement(ctrct, "AsstClss").text    = rec["2.11_classe_actifs"]
        ET.SubElement(ctrct, "CryptoBsd").text   = rec["2.12_crypto_based"]
        ET.SubElement(ctrct, "UndrlygId").text   = rec["2.13_id_sous_jacent"]
        ET.SubElement(ctrct, "UndrlygIdTp").text = rec["2.13_type_id"]

        # Valeur notionnelle
        ntl = ET.SubElement(tx_rpt, "TxData")
        ET.SubElement(ntl, "NtnlAmt").text  = str(rec["2.7_montant_notionnel"])
        ET.SubElement(ntl, "NtnlCcy").text  = rec["2.6_devise_notionnel"]
        ET.SubElement(ntl, "PricOrRt").text = rec["2.5_prix_taux"]

        # Informations DeFi spécifiques
        defi = ET.SubElement(tx_rpt, "DeFiData")
        ET.SubElement(defi, "Protocole").text   = "Morpho Blue"
        ET.SubElement(defi, "Reseau").text      = "Ethereum Mainnet"
        ET.SubElement(defi, "TxRef").text       = rec["ref_reglement"]
        ET.SubElement(defi, "Bloc").text        = str(rec["bloc"])
        ET.SubElement(defi, "TsOp").text        = rec["timestamp_operation"]
        ET.SubElement(defi, "GasFeesETH").text  = str(rec["gas_fees_eth"])
        ET.SubElement(defi, "EmirScope").text   = rec["emir_scope"]
        ET.SubElement(defi, "MiCAClss").text    = rec["mica_classification_loan"]

    # Formatage
    xml_str = ET.tostring(root, encoding="unicode")
    xml_pretty = parseString(xml_str).toprettyxml(indent="  ")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(xml_pretty)

    print(f"  💾 Rapport EMIR XML  : {output_file}")
    return xml_pretty


# ═══════════════════════════════════════════════════════════════════════════════
# RAPPORT MiCA
# ═══════════════════════════════════════════════════════════════════════════════

def generer_rapport_mica(evenements: list[dict]) -> dict:
    """
    Génère une synthèse de conformité MiCA pour les actifs utilisés
    dans les opérations Morpho Blue.

    MiCA distingue :
    - Protocoles entièrement décentralisés → exemptés (Morpho Blue)
    - E-money tokens (EMT) → soumis à MiCA (USDC, USDT, EURCV)
    - Asset-referenced tokens (ART) → soumis à MiCA (DAI)
    - Autres crypto-actifs → soumis à MiCA selon conditions (WETH, wstETH, WBTC)

    Ce rapport identifie les actifs utilisés et leur classification MiCA,
    pour faciliter la due diligence compliance de l'institution.
    """
    actifs_utilises = {}

    for evt in evenements:
        mp = evt.get("market_params")
        if not mp:
            continue
        for token_addr, token_type in [
            (mp.get("loan_token"), "LOAN"),
            (mp.get("coll_token"), "COLLATERAL"),
        ]:
            if token_addr:
                addr_cs = Web3.to_checksum_address(token_addr)
                info = TOKEN_INFO.get(addr_cs)
                if info and addr_cs not in actifs_utilises:
                    actifs_utilises[addr_cs] = {
                        "symbol":      info["symbol"],
                        "type":        token_type,
                        "mica_type":   info.get("mica_type", "UNKNOWN"),
                        "mica_issuer": info.get("mica_issuer"),
                        "isin":        info.get("isin"),
                        "obligations": _obligations_mica(info.get("mica_type", "")),
                    }

    return {
        "timestamp":         datetime.utcnow().isoformat(),
        "protocole":         "Morpho Blue",
        "protocole_statut":  "EXEMPTÉ_MiCA",
        "protocole_note":    ("Morpho Blue est un protocole entièrement décentralisé "
                              "sans entité opératrice identifiable — probablement "
                              "exempté de MiCA en tant que service DeFi."),
        "actifs_identifies": actifs_utilises,
        "nb_actifs_emt":     sum(1 for v in actifs_utilises.values()
                                 if v["mica_type"] == "EMT"),
        "nb_actifs_art":     sum(1 for v in actifs_utilises.values()
                                 if v["mica_type"] == "ART"),
        "nb_actifs_crypto":  sum(1 for v in actifs_utilises.values()
                                 if v["mica_type"] == "CRYPTO"),
    }


def _obligations_mica(mica_type: str) -> str:
    """Retourne les obligations MiCA selon le type d'actif."""
    obligations = {
        "EMT": ("Émetteur doit détenir un agrément MiCA en tant qu'établissement "
                "de monnaie électronique. Vérifier l'agrément et les réserves de couverture."),
        "ART": ("Émetteur doit détenir un agrément MiCA spécifique ART. "
                "Vérifier le livre blanc MiCA et les obligations de réserve."),
        "CRYPTO": ("Soumis à MiCA selon les conditions d'offre au public et "
                   "d'admission à la négociation. Vérifier la qualification."),
    }
    return obligations.get(mica_type, "Qualification MiCA à déterminer.")


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("SCRIPT 10 — REPORTING RÉGLEMENTAIRE EMIR/SFTR")
    print("Depuis les événements on-chain Morpho Blue")
    print("Section IV.5 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*62)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")
    print(f"   Institution : {NOM_INSTITUTION}")
    print(f"   LEI         : {LEI_INSTITUTION}")

    # ── Avertissement réglementaire ────────────────────────────────────────────
    print(f"\n{'─'*62}")
    print("⚠️  AVERTISSEMENT RÉGLEMENTAIRE")
    print("   Ce script génère des données structurées compatibles EMIR/SFTR.")
    print("   Il NE constitue PAS un avis juridique sur la qualification")
    print("   réglementaire des opérations Morpho Blue.")
    print("   Un avis juridique spécialisé est requis avant toute déclaration.")
    print(f"{'─'*62}")

    WALLET = os.getenv(
        "WALLET_INSTITUTION",
        "0x4e9d257FfEce3C9fAb9D8D5e4e6e14C98E6b6b6b"
    )

    # ── Extraction des événements ──────────────────────────────────────────────
    print(f"\n{'═'*62}")
    print("ÉTAPE 1 — EXTRACTION DES ÉVÉNEMENTS ON-CHAIN")
    print(f"{'═'*62}")
    evenements = extraire_evenements(
        wallet=WALLET,
        nb_blocs=7200,  # ~24h
        types=["Supply", "Borrow", "Withdraw", "Repay"],
    )

    if not evenements:
        print("\n  ℹ️  Aucun événement trouvé sur les dernières 24h.")
        print("     Vérifier l'adresse du wallet et la période de recherche.")
        exit(0)

    # ── Qualification réglementaire ────────────────────────────────────────────
    print(f"\n{'═'*62}")
    print("ÉTAPE 2 — QUALIFICATION RÉGLEMENTAIRE")
    print(f"{'═'*62}")
    exemple_qualif = qualifier_operation_emir("SUPPLY")
    print(f"\n  EMIR scope  : {exemple_qualif['emir_scope']}")
    print(f"  SFTR scope  : {exemple_qualif['sftr_scope']}")
    print(f"  MiCA scope  : {exemple_qualif['mica_scope']}")
    print(f"  Champ 2.12  : {exemple_qualif['champ_2_12']}")
    print(f"\n  ⚠️  {exemple_qualif['avis_note']}")

    # ── Rapport EMIR CSV + XML ─────────────────────────────────────────────────
    print(f"\n{'═'*62}")
    print("ÉTAPE 3 — GÉNÉRATION DES FICHIERS DE REPORTING")
    print(f"{'═'*62}")

    records  = generer_rapport_emir_csv(evenements, "emir_report.csv")
    xml_str  = generer_rapport_xml(records, "emir_report.xml")

    # ── Rapport MiCA ───────────────────────────────────────────────────────────
    rapport_mica = generer_rapport_mica(evenements)
    mica_file    = "mica_compliance_report.json"
    with open(mica_file, "w") as f:
        json.dump(rapport_mica, f, indent=2, default=str)
    print(f"  💾 Rapport MiCA JSON : {mica_file}")

    # ── Synthèse finale ────────────────────────────────────────────────────────
    print(f"\n{'━'*62}")
    print("SYNTHÈSE")
    print(f"{'━'*62}")
    print(f"  Événements traités     : {len(evenements)}")
    print(f"  Enregistrements EMIR   : {len(records)}")
    print(f"  Actifs EMT identifiés  : {rapport_mica['nb_actifs_emt']}")
    print(f"  Actifs ART identifiés  : {rapport_mica['nb_actifs_art']}")
    print(f"  Actifs Crypto identif. : {rapport_mica['nb_actifs_crypto']}")

    print(f"\n  FICHIERS GÉNÉRÉS :")
    print(f"    emir_report.csv          → Import dans le système de reporting EMIR")
    print(f"    emir_report.xml          → Soumission trade repository (XML ISO 20022)")
    print(f"    mica_compliance_report.json → Due diligence compliance MiCA")

    print(f"\n  PROCHAINES ÉTAPES :")
    for step in [
        "1. Faire valider la qualification EMIR/SFTR par l'équipe juridique",
        "2. Vérifier les agréments MiCA des émetteurs d'EMT (USDC, USDT)",
        "3. Adapter le format UTI selon les exigences du trade repository",
        "4. Soumettre le fichier XML au trade repository (DTCC, Regis-TR, UnaVista)",
        "5. Conserver les fichiers de reporting pendant 5 ans (obligation EMIR)",
    ]:
        print(f"    {step}")
    print("━"*62)