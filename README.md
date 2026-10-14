# Bot réservation BU Joseph-Fourier

Réserve **RDC Sud n°4**, de **11h30 à 14h30**, du lundi au vendredi, **14 jours à l'avance**.
Il valide aussi tout seul le mail de confirmation d'Affluences.

## Installation (une seule fois)

1. Crée un dépôt GitHub **privé** (ex : `bot-bu`).
2. Mets dedans `reserver.py` et le dossier `.github/workflows/reserver.yml`.
3. Crée un mot de passe d'application Gmail :
   https://myaccount.google.com/apppasswords
   (il faut la validation en 2 étapes activée).
4. Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret** :
   - `AFF_EMAIL` : ton adresse Gmail
   - `IMAP_PASS` : le mot de passe d'application (16 lettres)
   - `TG_TOKEN` et `TG_CHAT` : optionnel, pour recevoir un message Telegram

## Tester

Onglet **Actions → Réservation BU → Run workflow**.
Mets une date libre (ex : `2026-10-28`) puis lance.
Regarde les logs : ils affichent la liste des salles trouvées et la réponse du serveur.

## Changer les réglages

En haut de `reserver.py` : `SALLES`, `HEURE_DEBUT`, `HEURE_FIN`, `JOURS_AVANCE`.
