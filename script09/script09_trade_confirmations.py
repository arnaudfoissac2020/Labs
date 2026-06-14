"""
SCRIPT 9 — Confirmation structurée depuis un tx hash Morpho Blue
          Équivalent fonctionnel d'un message SWIFT MT54x

Contexte : En finance traditionnelle, la confirmation d'un règlement-livraison
           est communiquée via des messages SWIFT de la famille MT54x :
           - MT540 : Receive Free (retrait de collatéral sans contrepartie cash)
           - MT541 : Receive Against Payment (supply — dépôt d'actifs contre rendement)
           - MT542 : Deliver Free (withdraw — retrait sans contrepartie)
           - MT543 : Deliver Against Payment (borrow — livraison de cash contre collatéral)

           Sur Morpho Blue, le tx hash constitue la référence de règlement
           irréfutable. Ce script génère un fichier de confirmation structuré
           (JSON + XML ISO 20022) à partir d'un tx hash, en extrayant et
           normalisant tous les attributs nécessaires au processus de
           confirmation back-office.

           Il inclut également une logique de retry automatique pour les
           transactions soumises mais non encore confirmées (pending),
           et une gestion des transactions revertées (avec diagnostic).

Mapping MT54x → Morpho Blue :
    Supply   → MT541 (Receive Against Payment)
    Withdraw → MT542 (Deliver Free)
    Borrow   → MT543 (Deliver Against Payment)
    Repay    → MT540 (Receive Free)
    SupplyCollateral   → MT541 (variante collatéral)
    WithdrawCollateral → MT542 (variante collatéral)

Dépendances :
    pip install web3 python-dotenv

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
  - SWIFT, MT540-MT543 Standards, SWIFT Standards Release 2024
  - BIS Quarterly Review, DeFi risks, décembre 2021
  - Morpho Docs, Variable Rate Market, 2025
"""

import os, sys
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.dom.minidom import parseString
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ─── ADRESSES ─────────────────────────────────────────────────────────────────

MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
CHAINLINK_ETH_USD   = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"

TOKEN_INFO = {
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": {"symbol": "USDC",   "decimals": 6},
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": {"symbol": "USDT",   "decimals": 6},
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": {"symbol": "WETH",   "decimals": 18},
    "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0": {"symbol": "wstETH", "decimals": 18},
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {"symbol": "WBTC",   "decimals": 8},
    "0x6B175474E89094C44Da98b954EedeAC495271d0F": {"symbol": "DAI",    "decimals": 18},
}

# Mapping opération Morpho → type de message MT54x
MT54X_MAPPING = {
    "Supply":             {"mt_type": "MT541", "description": "Receive Against Payment — dépôt d'actifs de prêt"},
    "Withdraw":           {"mt_type": "MT542", "description": "Deliver Free — retrait d'actifs de prêt"},
    "Borrow":             {"mt_type": "MT543", "description": "Deliver Against Payment — emprunt contre collatéral"},
    "Repay":              {"mt_type": "MT540", "description": "Receive Free — remboursement de dette"},
    "SupplyCollateral":   {"mt_type": "MT541", "description": "Receive Against Payment — dépôt de collatéral"},
    "WithdrawCollateral": {"mt_type": "MT542", "description": "Deliver Free — retrait de collatéral"},
}

# ─── ABIs ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.morpho_abis import MORPHO_EVENTS_ABI, MORPHO_PARAMS_ABI, CHAINLINK_ABI

# ═══════════════════════════════════════════════════════════════════════════════
# FONCTIONS UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

def get_eth_price_usd() -> float | None:
    """Lit le prix ETH/USD depuis Chainlink."""
    try:
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_ETH_USD),
            abi=CHAINLINK_ABI
        )
        return feed.functions.latestRoundData().call()[1] / 1e8
    except Exception:
        return None


def generer_uti(tx_hash: str, event_type: str, index: int = 0) -> str:
    """Génère un UTI interne depuis le tx hash."""
    short = tx_hash[2:10].upper()
    suffix = f"-{index:02d}" if index > 0 else ""
    return f"MORPHO-{event_type[:3]}-{short}{suffix}"


def lookup_token(address: str) -> dict:
    """Résout une adresse token ERC-20 en symbole/décimales."""
    addr_cs = Web3.to_checksum_address(address)
    return TOKEN_INFO.get(addr_cs, {"symbol": f"UNKNOWN_{address[:8]}", "decimals": 18})


def get_market_params(market_id_bytes: bytes) -> dict | None:
    """Lit les paramètres immuables d'un marché Morpho."""
    try:
        morpho = w3.eth.contract(
            address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
            abi=MORPHO_PARAMS_ABI
        )
        params = morpho.functions.idToMarketParams(market_id_bytes).call()
        loan_info = lookup_token(params[0])
        coll_info = lookup_token(params[1])
        return {
            "loan_token":       params[0],
            "collateral_token": params[1],
            "oracle":           params[2],
            "irm":              params[3],
            "lltv":             params[4] / 1e18,
            "loan_symbol":      loan_info["symbol"],
            "loan_decimals":    loan_info["decimals"],
            "coll_symbol":      coll_info["symbol"],
            "coll_decimals":    coll_info["decimals"],
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION ET DÉCODAGE DE LA TRANSACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extraire_infos_transaction(tx_hash: str) -> dict:
    """
    Extrait les informations de base d'une transaction Ethereum :
    statut, bloc, timestamp, gas fees.

    Trois statuts possibles :
    - CONFIRMED   : transaction incluse dans un bloc avec statut = 1
    - REVERTED    : transaction incluse mais revertée (statut = 0)
    - NOT_FOUND   : transaction inconnue (hash inexistant ou pas encore minée)

    Args:
        tx_hash : hash de la transaction (0x...)

    Returns:
        Dictionnaire avec les métadonnées de la transaction
    """
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)

        if receipt is None:
            return {"statut": "NOT_FOUND", "tx_hash": tx_hash}

        block    = w3.eth.get_block(receipt["blockNumber"])
        tx       = w3.eth.get_transaction(tx_hash)
        eth_price = get_eth_price_usd()

        # Calcul des gas fees
        gas_used      = receipt["gasUsed"]
        gas_price_wei = receipt.get("effectiveGasPrice", tx.get("gasPrice", 0))
        gas_fees_eth  = gas_used * gas_price_wei / 1e18
        gas_fees_usd  = gas_fees_eth * eth_price if eth_price else None

        # Timestamp du bloc
        block_ts = block["timestamp"]
        dt_utc   = datetime.fromtimestamp(block_ts, tz=timezone.utc)

        # Statut Ethereum (1 = succès, 0 = revert)
        statut = "CONFIRMED" if receipt["status"] == 1 else "REVERTED"

        # Finalité Ethereum (~12-15 min = 2 checkpoints d'époque)
        bloc_actuel = w3.eth.block_number
        confirmations = bloc_actuel - receipt["blockNumber"]
        est_final = confirmations >= 64  # ~12-15 min pour 2 checkpoints

        return {
            "statut":           statut,
            "tx_hash":          tx_hash,
            "bloc":             receipt["blockNumber"],
            "bloc_hash":        block["hash"].hex(),
            "confirmations":    confirmations,
            "est_final":        est_final,
            "timestamp_utc":    dt_utc.isoformat(),
            "date_reglement":   dt_utc.strftime("%Y-%m-%d"),
            "heure_reglement":  dt_utc.strftime("%H:%M:%S UTC"),
            "gas_used":         gas_used,
            "gas_price_gwei":   round(gas_price_wei / 1e9, 4),
            "gas_fees_eth":     round(gas_fees_eth, 8),
            "gas_fees_usd":     round(gas_fees_usd, 4) if gas_fees_usd else None,
            "eth_price_usd":    eth_price,
            "from":             tx["from"],
            "to":               tx.get("to"),
        }

    except Exception as e:
        return {
            "statut":  "ERROR",
            "tx_hash": tx_hash,
            "erreur":  str(e),
        }


def decoder_evenements_morpho(tx_hash: str, tx_receipt: dict) -> list[dict]:
    """
    Décode tous les événements Morpho Blue contenus dans une transaction.

    Une transaction peut contenir plusieurs événements Morpho (ex. une
    opération atomique supply + borrow dans la même tx via callbacks).

    Args:
        tx_hash    : hash de la transaction
        tx_receipt : reçu de transaction déjà récupéré

    Returns:
        Liste des événements Morpho décodés et normalisés
    """
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_EVENTS_ABI
    )

    evenements = []
    index = 0

    for event_name in MT54X_MAPPING:
        try:
            decoded = morpho.events[event_name]().process_receipt(tx_receipt)

            for evt in decoded:
                args        = evt["args"]
                market_id   = "0x" + args["id"].hex()
                market_params = get_market_params(args["id"])

                # Détermination du token et des décimales selon le type
                if event_name in ("SupplyCollateral", "WithdrawCollateral"):
                    token_symbol = market_params["coll_symbol"] if market_params else "UNKNOWN"
                    decimals     = market_params["coll_decimals"] if market_params else 18
                else:
                    token_symbol = market_params["loan_symbol"] if market_params else "UNKNOWN"
                    decimals     = market_params["loan_decimals"] if market_params else 18

                montant = args.get("assets", 0) / (10**decimals)
                shares  = args.get("shares", 0) / 1e18

                mt_info = MT54X_MAPPING[event_name]

                evenements.append({
                    "index":        index,
                    "event_type":   event_name.upper(),
                    "uti":          generer_uti(tx_hash, event_name.upper(), index),
                    "mt_type":      mt_info["mt_type"],
                    "mt_desc":      mt_info["description"],
                    "market_id":    market_id,
                    "marche":       (f"{market_params['loan_symbol']}/"
                                    f"{market_params['coll_symbol']} "
                                    f"(LLTV {market_params['lltv']*100:.1f}%)"
                                    if market_params else market_id[:16] + "..."),
                    "caller":       args.get("caller", ""),
                    "on_behalf":    args.get("onBehalf", ""),
                    "receiver":     args.get("receiver", args.get("onBehalf", "")),
                    "montant":      round(montant, 8),
                    "shares":       round(shares, 8),
                    "token_symbol": token_symbol,
                    "market_params": market_params,
                })
                index += 1

        except Exception:
            continue

    return evenements


# ═══════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION DU FICHIER DE CONFIRMATION (JSON)
# ═══════════════════════════════════════════════════════════════════════════════

def generer_confirmation_json(
    tx_infos: dict,
    evenements: list[dict],
) -> dict:
    """
    Génère la confirmation structurée au format JSON.

    La structure reprend les champs standards d'un message MT54x
    adaptés au contexte Morpho Blue, tels que décrits dans le
    tableau de correspondance de la Section IV.5 du mémoire.
    """
    confirmations = []

    for evt in evenements:
        confirmations.append({

            # ── IDENTIFICATION ──────────────────────────────────────────────
            "reference_interne":  evt["uti"],
            "reference_note":     ("UTI interne format MORPHO-{TYPE}-{HASH}. "
                                   "Conversion UTI EMIR en production requise."),
            "type_message":       evt["mt_type"],
            "type_operation":     evt["event_type"],
            "description":        evt["mt_desc"],

            # ── RÈGLEMENT ───────────────────────────────────────────────────
            # Sur Morpho, le règlement est atomique :
            # il n'y a pas de délai T+2 — la confirmation est disponible
            # dès l'inclusion du bloc (et définitive après finalité ~12-15 min)
            "statut_reglement":   tx_infos["statut"],
            "date_reglement":     tx_infos.get("date_reglement", ""),
            "heure_reglement":    tx_infos.get("heure_reglement", ""),
            "timestamp_utc":      tx_infos.get("timestamp_utc", ""),
            "type_reglement":     "ATOMIC_DvP_ONCHAIN",
            "type_reglement_note": ("Règlement atomique on-chain — livraison et "
                                    "paiement dans la même transaction. "
                                    "Pas de risque de contrepartie au règlement."),
            "est_final":          tx_infos.get("est_final", False),
            "finalite_note":      ("Finalité Ethereum atteinte après 2 checkpoints "
                                   "d'époque (~12-15 min). "
                                   f"Confirmations actuelles : "
                                   f"{tx_infos.get('confirmations', 0)} blocs."),

            # ── RÉFÉRENCES DE RÈGLEMENT ─────────────────────────────────────
            # Le tx_hash est la référence de règlement irréfutable —
            # équivalent fonctionnel de la référence SWIFT MT54x
            "ref_reglement_onchain":     tx_infos["tx_hash"],
            "ref_reglement_note":        ("Équivalent fonctionnel d'une référence "
                                          "SWIFT MT54x — preuve cryptographique "
                                          "irréfutable du règlement."),
            "bloc":                      tx_infos.get("bloc", ""),
            "bloc_hash":                 tx_infos.get("bloc_hash", ""),

            # ── INSTRUMENT / MARCHÉ ─────────────────────────────────────────
            "market_id":          evt["market_id"],
            "marche":             evt["marche"],
            "isin_interne":       f"MORPHO_{evt.get('token_symbol', 'UNKNOWN')}",
            "isin_note":          ("Pas d'ISIN officiel — convention interne. "
                                   "Référentiel de mapping requis en production."),
            "contrepartie":       MORPHO_BLUE_ADDRESS,
            "contrepartie_lei":   "MORPHO_BLUE_ETH_MAINNET",
            "contrepartie_note":  ("Smart contract sans LEI officiel — "
                                   "convention interne requise."),

            # ── MONTANT ─────────────────────────────────────────────────────
            "montant":            evt["montant"],
            "token_symbol":       evt["token_symbol"],
            "shares":             evt["shares"],
            "caller":             evt["caller"],
            "on_behalf":          evt["on_behalf"],
            "receiver":           evt["receiver"],

            # ── FRAIS DE RÈGLEMENT ──────────────────────────────────────────
            # Les gas fees sont le coût du règlement on-chain —
            # à comptabiliser comme frais opérationnels (non inclus dans le taux)
            "gas_fees_eth":       tx_infos.get("gas_fees_eth", 0),
            "gas_fees_usd":       tx_infos.get("gas_fees_usd"),
            "gas_note":           ("Gas fees = coût opérationnel de règlement. "
                                   "À comptabiliser en charges distinctes "
                                   "selon IFRS 9 — cf. Section IV.5."),

            # ── RÉGLEMENTAIRE ───────────────────────────────────────────────
            "protocole":          "Morpho Blue",
            "reseau":             "Ethereum Mainnet",
            "emir_scope":         ("À qualifier — Supply/Borrow hors scope EMIR "
                                   "probable. Cf. Section IV.5 du mémoire."),
        })

    return {
        "type":         "MORPHO_BLUE_SETTLEMENT_CONFIRMATION",
        "version":      "1.0",
        "genere_le":    datetime.utcnow().isoformat(),
        "tx_hash":      tx_infos["tx_hash"],
        "statut":       tx_infos["statut"],
        "nb_operations": len(confirmations),
        "confirmations": confirmations,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GÉNÉRATION DU FICHIER DE CONFIRMATION (XML ISO 20022)
# ═══════════════════════════════════════════════════════════════════════════════

def generer_confirmation_xml(confirmation_json: dict) -> str:
    """
    Génère la confirmation au format XML ISO 20022.

    L'ISO 20022 est le standard de messagerie financière qui remplace
    progressivement SWIFT MT dans les systèmes de règlement-livraison
    (TARGET2-Securities, DTCC). Son adoption dans le contexte DeFi
    institutionnel facilite l'intégration avec les systèmes existants.

    Format : SctiesSttlmTxConf (Securities Settlement Transaction Confirmation)

    Args:
        confirmation_json : dictionnaire de confirmation généré par
                           generer_confirmation_json()

    Returns:
        String XML ISO 20022 formaté
    """
    root = ET.Element("Document")
    root.set("xmlns", "urn:iso:std:iso:20022:tech:xsd:sese.025.001.10")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")

    sctis = ET.SubElement(root, "SctiesSttlmTxConf")

    # ── Header ─────────────────────────────────────────────────────────────────
    hdr = ET.SubElement(sctis, "MsgHdr")
    ET.SubElement(hdr, "MsgId").text = confirmation_json["tx_hash"][:20]
    ET.SubElement(hdr, "CreDtTm").text = confirmation_json["genere_le"]

    for i, conf in enumerate(confirmation_json.get("confirmations", [])):

        tx_conf = ET.SubElement(sctis, "TxConf")

        # Références
        refs = ET.SubElement(tx_conf, "Refs")
        ET.SubElement(refs, "AcctOwnrTxId").text = conf["reference_interne"]
        ET.SubElement(refs, "MktInfrstrctrTxId").text = conf["ref_reglement_onchain"]

        # Statut du règlement
        sttlm_sts = ET.SubElement(tx_conf, "SttlmSts")
        ET.SubElement(sttlm_sts, "Cd").text = (
            "STLD" if conf["statut_reglement"] == "CONFIRMED" else
            "CANC" if conf["statut_reglement"] == "REVERTED" else "PENL"
        )

        # Date et heure de règlement
        sttld = ET.SubElement(tx_conf, "SttlmDtls")
        sttlm_dt = ET.SubElement(sttld, "SttlmDt")
        ET.SubElement(sttlm_dt, "Dt").text = conf.get("date_reglement", "")
        ET.SubElement(sttld, "SttlmTm").text = conf.get("heure_reglement", "")

        # Type de règlement
        ET.SubElement(sttld, "SttlmTp").text = conf["type_reglement"]

        # Instrument financier (marché Morpho)
        fin_instr = ET.SubElement(tx_conf, "FinInstrmId")
        ET.SubElement(fin_instr, "OthrId").text = conf["market_id"]
        ET.SubElement(fin_instr, "OthrDesc").text = conf["marche"]

        # Quantité
        qty = ET.SubElement(tx_conf, "SttlmQty")
        ET.SubElement(qty, "Qty").text = str(conf["montant"])
        ET.SubElement(qty, "Ccy").text = conf["token_symbol"]

        # Contrepartie (Morpho Blue smart contract)
        cparty = ET.SubElement(tx_conf, "CntrPty")
        ET.SubElement(cparty, "SfkpgAcct").text = MORPHO_BLUE_ADDRESS
        ET.SubElement(cparty, "AcctOwnr").text = conf["contrepartie_lei"]

        # Gas fees (frais de règlement)
        chrgs = ET.SubElement(tx_conf, "RgltryCharg")
        ET.SubElement(chrgs, "Tp").text = "GAS_FEES_ETH"
        ET.SubElement(chrgs, "Amt").text = str(conf.get("gas_fees_eth", 0))
        if conf.get("gas_fees_usd"):
            ET.SubElement(chrgs, "AmtUSD").text = str(conf["gas_fees_usd"])

        # Notes spécifiques DeFi
        addl = ET.SubElement(tx_conf, "AddtlInf")
        ET.SubElement(addl, "Prtry").text = conf["type_reglement_note"]
        ET.SubElement(addl, "Bloc").text = str(conf["bloc"])
        ET.SubElement(addl, "BlocHash").text = conf.get("bloc_hash", "")
        ET.SubElement(addl, "EmirScope").text = conf["emir_scope"]

    # Formatage XML avec indentation
    xml_str = ET.tostring(root, encoding="unicode")
    return parseString(xml_str).toprettyxml(indent="  ")


# ═══════════════════════════════════════════════════════════════════════════════
# LOGIQUE DE RETRY
# ═══════════════════════════════════════════════════════════════════════════════

def attendre_confirmation(
    tx_hash: str,
    timeout_s: int = 300,
    intervalle_s: int = 12,
) -> dict:
    """
    Attend la confirmation d'une transaction en attente (pending).

    Stratégie :
    - Interroge le reçu de transaction toutes les 12 secondes (≈ 1 bloc)
    - Timeout après N secondes (défaut : 5 minutes)
    - Si toujours non confirmée après timeout : recommander le renvoi
      avec un gas price plus élevé

    Args:
        tx_hash     : hash de la transaction à surveiller
        timeout_s   : délai maximum d'attente en secondes
        intervalle_s: intervalle entre deux checks

    Returns:
        Résultat de la confirmation ou statut TIMEOUT
    """
    debut = time.time()
    tentatives = 0

    print(f"\n  ⏳ Attente de confirmation pour {tx_hash[:20]}...")
    print(f"     Timeout : {timeout_s}s | Intervalle : {intervalle_s}s")

    while (time.time() - debut) < timeout_s:
        tentatives += 1
        elapsed = int(time.time() - debut)

        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)

            if receipt is not None:
                statut = "CONFIRMED" if receipt["status"] == 1 else "REVERTED"
                print(f"  ✅ Transaction {statut} après {elapsed}s "
                      f"({tentatives} vérifications) — "
                      f"Bloc #{receipt['blockNumber']:,}")
                return extraire_infos_transaction(tx_hash)

            print(f"  [{elapsed:>4}s] Tentative {tentatives:>3} — "
                  f"En attente de confirmation...", end="\r")

        except Exception as e:
            print(f"  [{elapsed:>4}s] Erreur RPC : {e}")

        time.sleep(intervalle_s)

    # Timeout atteint
    gas_price_actuel = w3.eth.gas_price / 1e9
    print(f"\n  ⚠️  TIMEOUT ({timeout_s}s) — Transaction toujours non confirmée")
    print(f"     Gas price actuel : {gas_price_actuel:.2f} Gwei")
    print(f"     Action recommandée : renvoyer la transaction avec gas price "
          f"× 1.2 ({gas_price_actuel * 1.2:.2f} Gwei)")

    return {
        "statut":    "TIMEOUT",
        "tx_hash":   tx_hash,
        "timeout_s": timeout_s,
        "action":    (f"Renvoyer avec gas price ≥ {gas_price_actuel * 1.2:.2f} Gwei "
                      f"(× 1.2 du prix actuel). "
                      f"Vérifier que la nonce est correcte."),
    }


def diagnostiquer_revert(tx_hash: str) -> dict:
    """
    Diagnostique la cause d'un revert de transaction Morpho Blue.

    Un revert peut avoir plusieurs causes sur Morpho :
    - Slippage dépassé (amountOutMinimum non atteint)
    - Solde insuffisant pour le gas
    - Utilisation du marché trop élevée (liquidité insuffisante pour le withdraw)
    - Position non liquidatable (tentative de liquidation sur position saine)
    - Gas limit insuffisant

    Cette fonction tente de rejouer la transaction via eth_call pour
    capturer le message d'erreur Solidity.

    Args:
        tx_hash : hash de la transaction revertée

    Returns:
        Dictionnaire avec la cause probable du revert et l'action recommandée
    """
    causes_connues = {
        "insufficient balance": "Solde insuffisant pour couvrir le montant + gas fees",
        "HEALTHY_POSITION":     "Tentative de liquidation sur une position saine (HF > 1)",
        "INSUFFICIENT_LIQUIDITY": "Liquidité insuffisante pour le withdraw — marché trop utilisé",
        "ZERO_ASSETS":          "Montant de l'opération nul ou invalide",
        "UNAUTHORIZED":         "L'adresse appelante n'est pas autorisée pour ce compte",
        "MAX_FEE_EXCEEDED":     "Frais de protocole supérieurs au maximum autorisé",
    }

    try:
        tx      = w3.eth.get_transaction(tx_hash)
        receipt = w3.eth.get_transaction_receipt(tx_hash)

        # Rejouer la transaction pour capturer le message d'erreur
        try:
            w3.eth.call(
                {
                    "from":  tx["from"],
                    "to":    tx["to"],
                    "data":  tx["input"],
                    "value": tx["value"],
                    "gas":   tx["gas"],
                },
                block_identifier=receipt["blockNumber"] - 1
            )
            raison_probable = "Indéterminée — la transaction rejoue correctement"
        except Exception as call_error:
            raison_probable = str(call_error)

        # Correspondance avec les causes connues
        cause_identifiee = None
        for pattern, description in causes_connues.items():
            if pattern.lower() in raison_probable.lower():
                cause_identifiee = description
                break

        actions = {
            "Solde insuffisant":         "Vérifier le solde du wallet et les approbations ERC-20",
            "HEALTHY_POSITION":          "La position n'est pas liquidatable — vérifier le HF",
            "INSUFFICIENT_LIQUIDITY":    "Réduire le montant du withdraw ou attendre que l'utilisation baisse",
            "ZERO_ASSETS":               "Vérifier que le montant est > 0 avant soumission",
            "UNAUTHORIZED":              "Vérifier les autorisations de compte (authorize() Morpho)",
            "MAX_FEE_EXCEEDED":          "Contacter l'équipe Morpho — changement de frais de protocole",
        }

        action = next(
            (v for k, v in actions.items() if cause_identifiee and k in cause_identifiee),
            "Analyser les logs de la transaction sur Etherscan pour plus de détails."
        )

        return {
            "tx_hash":          tx_hash,
            "statut":           "REVERTED",
            "gas_consomme":     receipt["gasUsed"],
            "gas_limite":       tx["gas"],
            "gas_utilise_pct":  round(receipt["gasUsed"] / tx["gas"] * 100, 2),
            "raison_brute":     raison_probable[:200],
            "cause_probable":   cause_identifiee or "Non identifiée automatiquement",
            "action":           action,
            "note_gas":         ("Gas fees perdus même si la transaction a revert — "
                                 "à comptabiliser en charges opérationnelles."),
        }

    except Exception as e:
        return {"statut": "ERREUR_DIAGNOSTIC", "erreur": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# FONCTION PRINCIPALE — GÉNÉRER LA CONFIRMATION COMPLÈTE
# ═══════════════════════════════════════════════════════════════════════════════

def confirmer_transaction(
    tx_hash: str,
    attendre_si_pending: bool = True,
    exporter_xml: bool = True,
) -> dict:
    """
    Génère la confirmation complète d'une transaction Morpho Blue.

    Workflow :
    1. Vérification du statut de la transaction
    2. Si PENDING : attente de confirmation avec retry (optionnel)
    3. Si REVERTED : diagnostic de la cause du revert
    4. Si CONFIRMED : décodage des événements et génération des confirmations
    5. Export JSON + XML ISO 20022

    Args:
        tx_hash             : hash de la transaction Ethereum
        attendre_si_pending : activer le retry si la transaction est pending
        exporter_xml        : générer également le fichier XML ISO 20022

    Returns:
        Dictionnaire de confirmation complet
    """
    print(f"\n{'═'*62}")
    print(f"CONFIRMATION DE TRANSACTION MORPHO BLUE")
    print(f"Tx hash : {tx_hash}")
    print(f"{'═'*62}")

    # ── Étape 1 : Récupérer les infos de la transaction ────────────────────
    tx_infos = extraire_infos_transaction(tx_hash)

    if tx_infos["statut"] == "NOT_FOUND":
        if attendre_si_pending:
            print("\n  ⏳ Transaction non trouvée — en attente de confirmation...")
            tx_infos = attendre_confirmation(tx_hash)
        else:
            print("\n  ❌ Transaction non trouvée (hash inexistant ou non minée)")
            return tx_infos

    if tx_infos["statut"] == "TIMEOUT":
        return tx_infos

    # ── Étape 2 : Traitement selon le statut ──────────────────────────────
    if tx_infos["statut"] == "REVERTED":
        print(f"\n  ❌ Transaction REVERTÉE — Bloc #{tx_infos.get('bloc', '?'):,}")
        print(f"     Gas consommé : {tx_infos.get('gas_fees_eth', 0):.6f} ETH "
              f"(perdus même en cas de revert)")
        diagnostic = diagnostiquer_revert(tx_hash)
        print(f"     Cause probable : {diagnostic.get('cause_probable', 'N/A')}")
        print(f"     Action         : {diagnostic.get('action', 'N/A')}")

        return {
            "type":       "REVERT_CONFIRMATION",
            "tx_hash":    tx_hash,
            "tx_infos":   tx_infos,
            "diagnostic": diagnostic,
        }

    # ── Étape 3 : Décoder les événements (transaction confirmée) ───────────
    print(f"\n  ✅ Transaction CONFIRMÉE")
    print(f"     Bloc            : #{tx_infos['bloc']:,}")
    print(f"     Timestamp       : {tx_infos['timestamp_utc']}")
    print(f"     Confirmations   : {tx_infos['confirmations']} blocs")
    print(f"     Finalité        : {'✅ Définitive' if tx_infos['est_final'] else '⏳ En cours (~12-15 min)'}")
    print(f"     Gas fees        : {tx_infos['gas_fees_eth']:.6f} ETH",
          end="")
    if tx_infos.get("gas_fees_usd"):
        print(f"  (${tx_infos['gas_fees_usd']:.2f} USD)")
    else:
        print()

    receipt   = w3.eth.get_transaction_receipt(tx_hash)
    evenements = decoder_evenements_morpho(tx_hash, receipt)

    if not evenements:
        print("\n  ℹ️  Aucun événement Morpho Blue dans cette transaction")
        return {"type": "NO_MORPHO_EVENT", "tx_hash": tx_hash, "tx_infos": tx_infos}

    print(f"\n  {len(evenements)} opération(s) Morpho Blue détectée(s) :")
    for evt in evenements:
        print(f"    [{evt['mt_type']}] {evt['event_type']:<22} "
              f"{evt['montant']:>14,.6f} {evt['token_symbol']:<8} "
              f"→ {evt['marche']}")

    # ── Étape 4 : Génération de la confirmation ────────────────────────────
    confirmation_json = generer_confirmation_json(tx_infos, evenements)

    # Export JSON
    json_file = f"confirmation_{tx_hash[2:10]}.json"
    with open(json_file, "w") as f:
        json.dump(confirmation_json, f, indent=2, default=str)
    print(f"\n  💾 Confirmation JSON : {json_file}")

    # Export XML ISO 20022
    if exporter_xml:
        xml_content = generer_confirmation_xml(confirmation_json)
        xml_file = f"confirmation_{tx_hash[2:10]}.xml"
        with open(xml_file, "w", encoding="utf-8") as f:
            f.write(xml_content)
        print(f"  💾 Confirmation XML  : {xml_file}")

    return confirmation_json


# ═══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("━"*62)
    print("SCRIPT 9 — CONFIRMATION STRUCTURÉE TX HASH MORPHO BLUE")
    print("Équivalent fonctionnel des messages SWIFT MT540-MT543")
    print("Section IV.5 — Mémoire DeFi institutionnelle sur Ethereum")
    print("━"*62)

    if not w3.is_connected():
        print("\n⚠️  Connexion RPC indisponible — vérifier RPC_URL dans .env")
        exit(1)

    print(f"\n✅ Connexion RPC établie — Bloc #{w3.eth.block_number:,}")

    # Hash de transaction à confirmer — passer en argument ou variable d'env
    TX_HASH = os.getenv(
        "TX_HASH",
        # Transaction Morpho Blue réelle à remplacer en production
        "0x0000000000000000000000000000000000000000000000000000000000000001"
    )

    result = confirmer_transaction(
        tx_hash=TX_HASH,
        attendre_si_pending=True,
        exporter_xml=True,
    )

    print(f"\n{'━'*62}")
    statut = result.get("statut", result.get("type", "UNKNOWN"))
    print(f"RÉSULTAT : {statut}")
    if isinstance(result.get("confirmations"), list):
        print(f"Opérations confirmées : {len(result['confirmations'])}")
    print("━"*62)