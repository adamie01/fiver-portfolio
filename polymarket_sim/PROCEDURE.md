# Cycle de simulation (toutes les 10 minutes) — argent fictif uniquement

Aucun pari réel, aucune connexion à un compte, aucun identifiant demandé.

1. `python3 polymarket_sim/sim.py review` — réévalue les paris ouverts (sortie si écart refermé ≤ 2 pts,
   si ma proba a bougé de > 5 pts, ou si fin dans l'heure), marque au prix exécutable (bids), vérifie le
   drawdown (stop à -40 % depuis le plus haut, ou cagnotte à 0). Si arrêt : bilan écrit dans `paris.txt`, notifier.
2. `python3 polymarket_sim/sim.py scan` — 1 000 marchés actifs max via Gamma ; écarte fin < 2 h et liquidité < 5 000 $.
3. Pour les paris ouverts : relire l'actualité, mettre à jour l'estimation (`estimate`).
4. Pour 2 à 4 marchés en tête de file non évalués : `show ID` (règles complètes), identifier la source officielle
   (sinon `skip`), rechercher l'actualité (X via recherche web `site:x.com`, presse, données publiques vérifiables),
   pondérer par crédibilité/fraîcheur, ignorer promo et bots, puis `estimate ID --p … --source … --reason …`.
5. (Recalculer d’abord le spot BTC si les échelles quotidiennes ont bougé d’~1k ou plus — sinon estimations périmées.) `python3 polymarket_sim/sim.py evaluate --execute` — écart > 8 pts vs ask, test de revente ≤ 3 % sur le carnet,
   demi-Kelly plafonné à 6 % de l'équité, 5 paris ouverts max ; l'entrée est écrite dans `paris.txt` avant d'être prise.

Frais taker modélisés : parts × taux × p × (1 − p), taux lu dans `feeSchedule` du marché, à l'achat et à la revente.
