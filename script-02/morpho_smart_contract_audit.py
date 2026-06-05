"""
SCRIPT 2 — Lecture annotée du smart contract Morpho Blue 
Contexte : Ce script illustre comment un ananlyste institutionnel
           peut lire et vérifier les propriétés de sécurité clés du contrat Morpho Blue
           déployé sur Ethereum mainnet, sans être développeur Solidity.

           Il combine deux approches complémentaires :
           - Des extraits Solidity annotés issus du code source officiel, commentés
             en langage risk manager pour identifier les vecteurs de risque et
             les protections en place
           - Des vérifications on-chain directes via Web3.py pour confirmer que
             le contrat déployé correspond au comportement décrit dans le whitepaper

Adresse Morpho Blue sur Ethereum mainnet :
    0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb

Dépendances :
    pip install web3 python-dotenv requests

Variables d'environnement (.env) :
    RPC_URL=https://mainnet.infura.io/v3/<YOUR_KEY>

Sources :
    - Morpho Blue Whitepaper : Gontier Delaunay et al., octobre 2023
    - Morpho Blue GitHub : https://github.com/morpho-org/morpho-blue
    - Audits : Spearbit, Trail of Bits, Certora — docs.morpho.org/get-started/resources/audits/
"""


# ═══════════════════════════════════════════════════════════════════════════════
# PARTIE 1 — EXTRAITS SOLIDITY ANNOTÉS
# ═══════════════════════════════════════════════════════════════════════════════

ANNOTATIONS = {

# ─────────────────────────────────────────────────────────────────────────────
# EXTRAIT 1 : Vérification de santé d'une position (isHealthy)
# Vecteur de risque principal : dépendance totale à l'oracle
# ─────────────────────────────────────────────────────────────────────────────
"isHealthy": {
    "titre": "Extrait 1 — Évaluation de la santé d'une position",
    "vecteur": "RISQUE ORACLE — dépendance totale au price feed du marché",
    "code": """
// ═══════════════════════════════════════════════════════════
// EXTRAIT 1 : Vérification de santé d'une position
// Source : Morpho Blue, MorphoLib.sol
// ═══════════════════════════════════════════════════════════

function isHealthy(
    MarketParams memory marketParams,
    Id id,
    address borrower
) internal view returns (bool) {

    // ⚠️ POINT DE RISQUE N°1 — Oracle
    // Le prix du collatéral est obtenu UNIQUEMENT depuis l'oracle
    // immuable spécifié à la création du marché.
    // Si cet oracle est :
    //   - Manipulé (flash loan attack) → prix artificiellement bas
    //     → des positions SAINES peuvent être liquidées abusivement
    //   - Défaillant (pas de mise à jour > heartbeat)
    //     → liquidations NÉCESSAIRES peuvent être bloquées
    //     → accumulation de bad debt non traitée
    // L'oracle ne peut JAMAIS être changé après déploiement du marché.
    uint256 collateralPrice = IOracle(marketParams.oracle).price();
    //                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    //                        Appel externe — point d'entrée unique pour
    //                        la valorisation du collatéral

    // Calcul du montant de dette accumulée (capital + intérêts composés)
    // Les intérêts s'accumulent à chaque bloc via le mécanisme de shares
    uint256 borrowed = uint256(position[id(marketParams)][borrower].borrowShares)
        .toAssetsUp(market[id].totalBorrowAssets, market[id].totalBorrowShares);

    // Calcul de la capacité d'emprunt maximale :
    // maxBorrow = (quantité de collatéral × prix oracle) × LLTV
    // ⚠️ Les deux facteurs multiplicatifs dépendent de l'oracle :
    //   - collateralPrice : prix unitaire du collatéral (oracle)
    //   - LLTV : fixé à la création du marché — IMMUABLE
    uint256 maxBorrow = uint256(
        position[id(marketParams)][borrower].collateral
    )
    .mulDivDown(collateralPrice, ORACLE_PRICE_SCALE)
    // ORACLE_PRICE_SCALE = 1e36 (normalisation des décimales)
    .wMulDown(marketParams.lltv);

    // ✅ PROTECTION : Comparaison simple et déterministe
    // Pas de logique complexe susceptible de bugs
    // La condition est binaire : soit saine, soit liquidatable
    return maxBorrow >= borrowed;
}
""",
    "analyse": """
ANALYSE — Extrait 1 (isHealthy)

Ce que fait cette fonction : elle détermine si une position est liquidatable.
Appelée lors de toute tentative de liquidation et lors des emprunts.

Point critique pour un prêteur/emprunteur institutionnel :
  → Si l'oracle retourne un prix anormalement bas (manipulation ou bug),
    des positions saines seront liquidées → perte de collatéral pour l'emprunteur
  → Si l'oracle ne met pas à jour son prix (staleness > heartbeat),
    des positions sous-collatéralisées peuvent échapper à la liquidation
    → accumulation de bad debt supportée proportionnellement par les prêteurs

Actions préventives recommandées :
  1. Vérifier la source et la fréquence de mise à jour de l'oracle
     de chaque marché cible via idToMarketParams() — cf. Extrait 4
  2. Configurer une alerte staleness > heartbeat configuré — cf. Script 7
  3. Préférer les marchés utilisant des oracles Chainlink multi-sources
     résistants aux flash loan attacks
"""
},

# ─────────────────────────────────────────────────────────────────────────────
# EXTRAIT 2 : Mécanisme de callback dans supply()
# Vecteur de risque : reentrancy potentielle via contrat tiers
# ─────────────────────────────────────────────────────────────────────────────
"supply_callback": {
    "titre": "Extrait 2 — Mécanisme de callback dans supply()",
    "vecteur": "RISQUE REENTRANCY — callback avant transfert de tokens",
    "code": """
// ═══════════════════════════════════════════════════════════
// EXTRAIT 2 : Mécanisme de callback dans supply()
// Source : Morpho Blue, Morpho.sol
// ═══════════════════════════════════════════════════════════

function supply(
    MarketParams memory marketParams,
    uint256 assets,
    uint256 shares,
    address onBehalf,
    bytes calldata data     // ← données passées au callback (optionnel)
) external returns (uint256, uint256) {

    // [... vérifications préliminaires ...]
    // [... mise à jour des intérêts accumulés ...]
    // [... calcul du nombre de shares à émettre ...]

    // ✅ PROTECTION N°1 — Mise à jour de l'état AVANT le callback
    // Le principe Checks-Effects-Interactions est respecté :
    // les positions internes sont mises à jour AVANT tout appel externe.
    // Un réentrant qui rappellerait supply() verrait les positions déjà
    // mises à jour — pas de double-comptage possible.
    position[marketId][onBehalf].supplyShares += shares.toUint128();
    market[marketId].totalSupplyShares += shares.toUint128();
    market[marketId].totalSupplyAssets += assets.toUint128();

    // ⚠️ POINT DE RISQUE N°2 — Callback externe
    // Si data.length > 0, le contrat à l'adresse onBehalf est appelé
    // avec la fonction onMorphoSupply() AVANT le transfert des tokens.
    // Un contrat malveillant pourrait tenter de réentrer dans Morpho.
    // PROTECTION : l'état a déjà été mis à jour (cf. ci-dessus).
    // LIMITE : si onBehalf est un contrat malveillant non audité,
    // des comportements inattendus peuvent survenir DANS ce contrat.
    if (data.length > 0) {
        IMorphoSupplyCallback(onBehalf).onMorphoSupply(assets, data);
        //                   ^^^^^^^^^
        //                   Appel externe — peut réentrer dans Morpho Blue
        //                   mais les états sont déjà mis à jour (protection)
    }

    // ✅ TRANSFERT après le callback — ordre sécurisé
    // Le transfert de tokens arrive EN DERNIER dans l'exécution.
    // Cela permet au callback d'utiliser les fonds reçus d'ailleurs
    // pour approvisionner le supply (ex: flash loan → supply atomique)
    IERC20(marketParams.loanToken).safeTransferFrom(
        msg.sender,
        address(this),
        assets
    );

    emit EventsLib.Supply(marketId, msg.sender, onBehalf, assets, shares);
    return (assets, shares);
}
""",
    "analyse": """
ANALYSE RISK MANAGER — Extrait 2 (supply callback)

Ce que fait ce mécanisme : il permet à un contrat tiers d'être notifié
d'un supply et d'agir en conséquence avant le transfert de tokens.
Cas d'usage légitime : leverage atomique, liquidations en une transaction.

Protection en place (✅ Checks-Effects-Interactions) :
  → Les états internes sont mis à jour AVANT le callback
  → Un réentrant verrait les positions déjà mises à jour
  → Pas de double-comptage possible dans Morpho Blue lui-même

Risque résiduel :
  → Un contrat tiers qui utilise le callback peut lui-même avoir
    des vulnérabilités — son audit est indépendant de celui de Morpho
  → Pour un institutionnel : ne pas activer les callbacks (data vide)
    pour les opérations simples de supply/repay
  → Ce risque a été vérifié par Spearbit et Trail of Bits —
    aucune vulnérabilité exploitable dans Morpho Blue lui-même
"""
},

# ─────────────────────────────────────────────────────────────────────────────
# EXTRAIT 3 : Liquidation et calcul du LIF
# Vecteur de risque : bad debt si liquidation insuffisante
# ─────────────────────────────────────────────────────────────────────────────
"liquidate_lif": {
    "titre": "Extrait 3 — Liquidation et calcul du LIF",
    "vecteur": "RISQUE BAD DEBT — gap de prix avant liquidation",
    "code": """
// ═══════════════════════════════════════════════════════════
// EXTRAIT 3 : Mécanisme de liquidation et calcul du LIF
// Source : Morpho Blue, Morpho.sol + MorphoLib.sol
// ═══════════════════════════════════════════════════════════

// ─── Calcul du Liquidation Incentive Factor ───────────────
// Source : Morpho Blue Whitepaper, Section 3.3
// LIF = min(maxLIF, 1 / (1 - cursor × (1 - LLTV)))
// avec maxLIF = 1.15 et cursor = 0.3
//
// Exemple : LLTV = 86% → LIF ≈ 1.047 (bonus liquidateur ≈ 4.7%)
//           LLTV = 77% → LIF ≈ 1.123 (bonus liquidateur ≈ 12.3%)
//           LLTV = 62% → LIF = 1.150 (plafonné à maxLIF)
//
// ⚠️ IMPLICATION RISK MANAGER :
// Plus le Loan To Value Limit (LLTV) est ÉLEVÉ (marché "sûr"), plus le LIF est FAIBLE.
// Avec un LIF de 1.047 sur un marché à LLTV=86%, un gap de prix
// de plus de 4.7% entre deux blocs peut générer de la bad debt
// car la récompense n'est plus suffisante pour inciter les liquidateurs.
uint256 incentiveFactor = UtilsLib.min(
    MAX_LIQUIDATION_INCENTIVE_FACTOR,  // = 1.15e18 (15% max)
    WAD.wDivDown(
        WAD - LIQUIDATION_CURSOR.wMulDown(WAD - marketParams.lltv)
        //    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        //    LIQUIDATION_CURSOR = 0.3e18
        //    WAD = 1e18 (représentation des nombres décimaux)
    )
);

function liquidate(
    MarketParams memory marketParams,
    address borrower,
    uint256 seizedAssets,   // montant de collatéral à saisir
    uint256 repaidShares,   // part de dette à rembourser
    bytes calldata data
) external returns (uint256, uint256) {

    // [... vérifications préliminaires ...]
    // [... mise à jour des intérêts accumulés ...]

    // ✅ Vérification que la position est effectivement liquidatable
    // DOIT être unhealthy — sinon revert
    require(
        !_isHealthy(marketParams, id, borrower),
        ErrorsLib.HEALTHY_POSITION
    );

    // ⚠️ POINT CRITIQUE — Calcul du collatéral à saisir
    // Le liquidateur reçoit (repaidAssets × incentiveFactor) en collatéral
    // Si le prix du collatéral a chuté brutalement (gap de prix),
    // la valeur du collatéral disponible peut être INFÉRIEURE au montant dû.
    // → Le liquidateur prend tout le collatéral disponible
    // → La dette résiduelle non couverte devient BAD DEBT
    uint256 seizedAssetsQuoted = repaidAssets.mulDivUp(
        incentiveFactor,
        collateralPrice
    );

    // ✅ PROTECTION ANTI-BANK-RUN — Traitement de la bad debt
    // Contrairement à Aave/Compound, Morpho comptabilise immédiatement
    // la bad debt : elle est socialisée proportionnellement entre
    // les prêteurs du marché — pas de panique sur les retraits possible
    // car la perte est reconnue instantanément, pas différée.
    if (position[id][borrower].collateral == 0) {
        uint256 badDebtShares = position[id][borrower].borrowShares;
        // Réduction proportionnelle du totalSupplyAssets de tous les prêteurs
        market[id].totalBorrowAssets =
            market[id].totalBorrowAssets.zeroFloorSub(badDebtAssets);
        market[id].totalSupplyAssets =
            market[id].totalSupplyAssets.zeroFloorSub(badDebtAssets);
        // ⚠️ Les prêteurs du marché supportent la perte — ISOLATION TOTALE
        // Cette perte N'EST PAS partagée avec d'autres marchés Morpho
    }

    emit EventsLib.Liquidate(
        id, msg.sender, borrower,
        repaidAssets, repaidShares,
        seizedAssets, badDebtAssets, badDebtShares
    );
    return (repaidAssets, seizedAssets);
}
""",
    "analyse": """
ANALYSE RISK MANAGER — Extrait 3 (liquidate + LIF)

Ce que fait ce mécanisme : il permet à n'importe quel acteur de liquider
une position dont le LTV a dépassé le LLTV du marché.

Points clés pour un prêteur institutionnel :

1. LE LIF DÉTERMINE LE NIVEAU DE PROTECTION
   LIF = min(1.15, 1 / (1 - 0.3 × (1 - LLTV)))
   → Sur un marché USDC/wstETH (LLTV=86%), LIF ≈ 1.05
   → Si le collatéral (wstETH) chute de plus de 5% en 1 bloc (12 sec),
     il peut y avoir de la bad debt MÊME SI le liquidateur agit vite
   → Facteur de risque à intégrer dans le sizing de la position

2. LA BAD DEBT EST SOCIALISÉE IMMÉDIATEMENT (✅ vs Aave/Compound)
   → Avantage : pas de bank run possible sur le marché (la perte est
     reconnue immédiatement, pas cachée dans le bilan du protocole)
   → Inconvénient : le prêteur institutionnel supporte directement
     sa part de perte proportionnelle — sans mutualisation inter-marchés

3. PAS DE CLOSE FACTOR (✅)
   → La totalité de la dette peut être remboursée en une seule liquidation
   → Réduit le risque d'accumulation progressive de bad debt

Actions préventives recommandées pour un prêteur :
  - Surveiller la volatilité intraday du collatéral (wstETH, WBTC)
  - Préférer les marchés à LLTV modéré (77-86%) vs très élevé (94-96%)
  - Intégrer la bad debt dans le calcul VaR via Expected Credit Loss (IFRS 9)
"""
},

# ─────────────────────────────────────────────────────────────────────────────
# EXTRAIT 4 : Paramètres immuables et vérification on-chain
# Propriété centrale : certitude contractuelle
# ─────────────────────────────────────────────────────────────────────────────
"market_params": {
    "titre": "Extrait 4 — Paramètres immuables du marché",
    "vecteur": "PROPRIÉTÉ CLÉE — immuabilité et certitude contractuelle",
    "code": """
// ═══════════════════════════════════════════════════════════
// EXTRAIT 4 : Structure des paramètres immuables d'un marché
// Source : Morpho Blue, interfaces/IMorpho.sol, MarketParamsLib.sol
// ═══════════════════════════════════════════════════════════

// Structure définissant les 5 paramètres immuables d'un marché Morpho Blue
// Une fois le marché créé, AUCUN de ces paramètres ne peut être modifié.
// La gouvernance MORPHO ne peut PAS modifier ces valeurs.
// (Source : Morpho Blue Whitepaper, Section 1.2)
struct MarketParams {
    address loanToken;       // Token prêté — ex: USDC (0xA0b8...)
    address collateralToken; // Token collatéral — ex: wstETH (0x7f39...)
    address oracle;          // Oracle de prix — ex: Chainlink MorphoChainlinkOracleV2
    address irm;             // Modèle de taux — ex: AdaptiveCurveIRM
    uint256 lltv;            // Seuil de liquidation — ex: 860000000000000000 (86%)
                             //                             ^^^^^^^^^^^^^^^^^^^^
                             //                             Représenté avec 18 décimales (WAD)
}

// ✅ L'identifiant unique d'un marché (Market ID) est le hash
// de ses paramètres immuables — garantissant que deux marchés
// aux paramètres identiques partagent le même ID.
function id(MarketParams memory marketParams) returns (Id) {
    return Id.wrap(keccak256(marketParams));
    //             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    //             Hash cryptographique — tout changement de paramètre
    //             produirait un ID différent → marché différent
}

// ✅ VÉRIFICATION ON-CHAIN RECOMMANDÉE
// Avant toute exposition institutionnelle, vérifier les paramètres
// directement on-chain via la fonction publique idToMarketParams() :
//
// morpho.idToMarketParams(marketId) → retourne les 5 paramètres immuables
//
// Cette vérification est la SEULE source de vérité fiable —
// les interfaces utilisateur peuvent être compromises.
// Le Script 2 implémente cette vérification dans la section ci-dessous.
""",
    "analyse": """
ANALYSE RISK MANAGER — Extrait 4 (paramètres immuables)

Ce que fait ce mécanisme : il définit l'identité contractuelle permanente
d'un marché Morpho Blue via le hash de ses 5 paramètres.

Pourquoi c'est une propriété de sécurité forte pour un institutionnel :

1. CERTITUDE CONTRACTUELLE ABSOLUE
   → Les paramètres que vous vérifiez aujourd'hui seront identiques
     à la clôture de votre position, quelle que soit l'évolution
     de la gouvernance MORPHO
   → Équivalent d'un contrat juridique sans clause de modification unilatérale

2. VÉRIFIABILITÉ DIRECTE ON-CHAIN
   → Toute interface utilisateur peut mentir — le contrat, non
   → La vérification on-chain via idToMarketParams() est la seule
     source de vérité acceptable pour une due diligence institutionnelle

3. POINT DE VIGILANCE : L'ORACLE EST AUSSI IMMUABLE
   → Si l'oracle d'un marché s'avère défaillant après déploiement,
     il ne peut pas être remplacé — seuls les prêteurs peuvent retirer
     leurs fonds du marché concerné
   → Vérifier l'oracle en priorité lors de la due diligence
"""
},

# ─────────────────────────────────────────────────────────────────────────────
# EXTRAIT 5 : Flash loans gratuits
# Vecteur de risque / feature : outil légitime ET vecteur d'attaque
# ─────────────────────────────────────────────────────────────────────────────
"flashloan": {
    "titre": "Extrait 5 — Flash loans gratuits (singleton)",
    "vecteur": "FEATURE & RISQUE — liquidations atomiques ET manipulation d'oracles spot",
    "code": """
// ═══════════════════════════════════════════════════════════
// EXTRAIT 5 : Flash loans gratuits sur Morpho Blue
// Source : Morpho Blue, Morpho.sol
// ═══════════════════════════════════════════════════════════

// ✅ ARCHITECTURE SINGLETON
// Tous les marchés Morpho Blue vivent dans UN SEUL contrat.
// Les flash loans ont donc accès à la liquidité de TOUS
// les marchés simultanément — sans frais.
// (Source : Morpho Blue Whitepaper, Section 4.1 + 4.3)
function flashLoan(
    address token,
    uint256 assets,
    bytes calldata data
) external {

    // ✅ Transfer des tokens au demandeur AVANT le callback
    // L'emprunteur reçoit les fonds et peut les utiliser
    IERC20(token).safeTransfer(msg.sender, assets);

    // ⚠️ POINT DE RISQUE N°3 — Flash loan + oracle spot
    // Pendant l'exécution du callback, si un marché utilise
    // un oracle spot (prix basé sur une pool AMM on-chain),
    // un acteur malveillant peut :
    //   1. Emprunter en flash loan une grande quantité du token
    //   2. Manipuler le prix spot de l'oracle
    //   3. Liquider abusivement des positions saines du marché
    //   4. Rembourser le flash loan
    // → Protection : utiliser des oracles résistants aux flash loans
    //   (Chainlink multi-sources, TWAP avec fenêtre temporelle)
    // → Morpho Blue lui-même ne protège PAS contre ce vecteur :
    //   c'est la responsabilité du créateur du marché de choisir
    //   un oracle adapté (cf. Extrait 4)
    IMorphoFlashLoanCallback(msg.sender).onMorphoFlashLoan(assets, data);

    // ✅ Vérification que le flash loan a bien été remboursé
    // Le solde du contrat doit être identique ou supérieur
    // à la situation avant le flash loan (0 frais sur Morpho Blue)
    uint256 finalBalance = IERC20(token).balanceOf(address(this));
    require(
        finalBalance >= initialBalance,
        ErrorsLib.REPAY_FAILED
    );
}
""",
    "analyse": """
ANALYSE RISK MANAGER — Extrait 5 (flash loans)

Double nature des flash loans sur Morpho Blue :

USAGE LÉGITIME (✅) :
  → Liquidations atomiques sans capital propre préalable
    (un liquidateur peut rembourser la dette d'un emprunteur
    en une seule transaction en utilisant un flash loan Morpho)
  → Levier / délevier en une transaction atomique
  → Arbitrage entre marchés
  → Ces usages améliorent l'efficacité des liquidations —
    bénéfique pour les prêteurs institutionnels

VECTEUR D'ATTAQUE (⚠️) :
  → Manipulation temporaire d'un oracle SPOT pendant l'exécution
    du callback → liquidations abusives sur le marché ciblé
  → Ce risque est indépendant de Morpho Blue lui-même —
    il dépend du choix de l'oracle par le créateur du marché

IMPLICATION POUR LA DUE DILIGENCE :
  → Systématiquement vérifier que l'oracle de tout marché cible
    est un oracle TWAP (average over time) ou multi-sources (Chainlink), pas un oracle
    spot basé sur une pool AMM (Uniswap TWAP court terme accepté,
    spot rejeté)
"""
},
}

