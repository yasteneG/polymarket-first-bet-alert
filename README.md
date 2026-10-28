# Alerte Polymarket : premier pari ≥ 10 000 $

Toutes les 5 minutes environ, GitHub Actions vérifie si un **nouveau compte** Polymarket vient de faire
un **premier pari de 10 000 $ ou plus**, et vous envoie une alerte Telegram.

Un pari exécuté en plusieurs morceaux (par exemple 4 000 $ + 6 000 $) est bien additionné :
on compte tous les achats du compte sur son premier marché pendant ses 30 premières minutes.

## Installation (environ 10 minutes)

### 1. Créer le bot Telegram
1. Dans Telegram, ouvrez **@BotFather**, envoyez `/newbot` et suivez les instructions.
   Il vous donne un **token** (du type `123456:ABC-DEF…`). Gardez-le.
2. Ouvrez la conversation avec votre nouveau bot et envoyez-lui n'importe quel message (par exemple « salut »).
3. Dans votre navigateur, ouvrez `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates`
   et repérez `"chat":{"id":123456789`. Ce nombre est votre **chat ID**.

### 2. Créer le dépôt GitHub (public)
1. Sur github.com, cliquez sur **New repository**, choisissez **Public** et créez-le.
2. Envoyez-y les fichiers de ce dossier (`alert.py`, `.github/workflows/alert.yml`, `.gitignore`, `README.md`).
   Le plus simple : **Add file → Upload files**, puis glissez-y le contenu du dossier.
   Le dossier `.github` est caché sur Mac : appuyez sur Cmd+Maj+. dans le Finder pour l'afficher.

### 3. Ajouter les secrets
Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_BOT_TOKEN` : le token du bot
- `TELEGRAM_CHAT_ID` : votre chat ID

Ces secrets restent invisibles même si le dépôt est public.

### 4. Lancer
Onglet **Actions → Polymarket first-bet alert → Run workflow** pour un premier essai.
Ensuite, le workflow tourne tout seul toutes les 5 minutes.

## Réglages (facultatif)
- **Changer le seuil** : Settings → Secrets and variables → Actions → onglet **Variables** →
  ajoutez `THRESHOLD_USD`, par exemple `25000`.
- Les autres paramètres se règlent en haut de `alert.py` : `MIN_FILL_USD`, `FIRST_BET_WINDOW_MIN`, `LOOKBACK_MIN`.

## Tester sur votre Mac
```bash
python3 alert.py
```
Sans les variables Telegram, les alertes s'affichent simplement dans le terminal.

## Limites
- **Délai** : GitHub lance les tâches planifiées avec 5 à 20 minutes de retard. Aucun pari n'est manqué
  (chaque passage regarde la dernière heure), mais l'alerte n'est pas instantanée.
- **Petits morceaux** : les exécutions de moins de 1 000 $ ne servent pas à repérer les candidats.
  Elles sont en revanche comptées dans le total final.
- **« Nouveau compte » ≠ « nouvelle personne »** : n'importe qui peut créer un nouveau portefeuille.
- Ce n'est pas un conseil d'investissement.
