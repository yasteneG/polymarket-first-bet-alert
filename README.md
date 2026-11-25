# Statistiques Polymarket : tous les paris ≥ 1 000 $

Toutes les 5 minutes, le script enregistre **tous les achats de 1 000 $ ou plus** faits sur Polymarket.
Chaque soir à **20 h (heure de Paris)**, il vérifie quels marchés ont été tranchés et vous envoie sur
Telegram un rapport :

- nombre d'achats suivis, gagnés, perdus, remboursés et encore en cours ;
- argent gagné ou perdu par les parieurs, taux de réussite et rendement ;
- un tableau gagnés / perdus selon le **prix d'entrée** (0-25 %, 25-50 %, 50-75 %, 75-90 %,
  90-95 %, 95-99 %, +99 %).

Un pari tranché est compté une fois pour toutes : seuls les marchés encore en cours sont revérifiés.

## Fonctionnement
- **Lancement** : cron-job.org lance le workflow toutes les 5 minutes (voir plus bas). Le planning
  intégré à GitHub reste actif en secours, mais il est irrégulier. Chaque passage reprend là où le
  précédent s'est arrêté, jusqu'à 24 h en arrière.
- **Mémoire** : les statistiques sont dans `stats.json`, gardé d'une exécution à l'autre par le cache
  de GitHub, et enregistré dans le dépôt après chaque rapport.
- **Rapport immédiat** : Actions → Polymarket stats → Run workflow, puis cochez
  « Envoyer le rapport tout de suite ».

## Installation
1. **Bot Telegram** : dans Telegram, ouvrez **@BotFather**, envoyez `/newbot`, puis gardez le token.
   Envoyez un message à votre bot, puis ouvrez `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire
   votre chat ID (`"chat":{"id":…`).
2. **Secrets du dépôt** (Settings → Secrets and variables → Actions) : `TELEGRAM_BOT_TOKEN` et
   `TELEGRAM_CHAT_ID`.
3. **Lancement toutes les 5 min** : sur cron-job.org, créez une tâche toutes les 5 minutes avec :
   - la méthode `POST` vers
     `https://api.github.com/repos/<vous>/<dépôt>/actions/workflows/alert.yml/dispatches` ;
   - les en-têtes `Authorization: Bearer <clé GitHub avec la permission Actions en lecture et écriture>`,
     `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` et
     `Content-Type: application/json` ;
   - le corps `{"ref":"main"}`.

## Réglages (facultatif)
Settings → Secrets and variables → Actions → onglet **Variables** : `MIN_BET_USD`, par exemple `5000`.

## Utilisation sur Mac
```bash
python3 alert.py --backfill 7 --report
```
Cette commande pré-remplit les statistiques avec les 7 derniers jours (environ 1 000 requêtes), puis
affiche le rapport dans le terminal si Telegram n'est pas configuré.

## Limites
- **Un « pari » = un achat** de 1 000 $ ou plus, côté preneur. Un même parieur peut en faire plusieurs.
  Les ventes ne sont pas comptées.
- **Gain ou perte** : le calcul suppose que le parieur garde sa position jusqu'au résultat.
- Ce n'est pas un conseil d'investissement.
