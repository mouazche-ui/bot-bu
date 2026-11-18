# Bot BU Joseph-Fourier

Version préparée le 18 novembre 2026. Connexion des comptes UGA par Chromium/Agalan,
puis lecture des seuls mails Affluences par l'interface web du serveur Zimbra.
Aucun IMAP, Gmail ou transfert de courrier. Les sessions restent en mémoire.

## État de validation

Le formulaire Agalan, les éléments du webmail connecté, le format réel des disponibilités
Affluences et les erreurs du workflow existant ont été inspectés. Les tests du programme
s'exécutent sans réseau et vérifient notamment les quotas, les doublons, les liens de
confirmation et les réponses de réservation incertaines.

La nouvelle connexion automatisée aux trois comptes et les appels webmail authentifiés
n'ont PAS encore été testés avec leurs secrets. La réservation et sa confirmation de
bout en bout ne sont pas certifiées. Le mode diagnostic doit valider cette étape en premier.
Ne pas présenter cette version comme déjà déployée ou fonctionnelle à 100 %.

## Installation sur le dépôt GitHub existant

1. Extraire cette archive. Mettre **le contenu** du dossier `bot-bu` à la racine du dépôt,
   notamment `reserver.py`, `requirements.txt`, le dossier `tests`, et
   `.github/workflows/reservation.yml`. Remplacer l'ancien script et l'ancien workflow.
   S'il existe un deuxième ancien workflow de réservation, le désactiver pour éviter
   deux bots simultanés. Les fichiers doivent être sur la branche par défaut.
2. Dans Settings → Secrets and variables → Actions, ajouter le secret `ACCOUNTS_JSON`.
   Copier le contenu de `accounts.example.json` et remplacer les trois identifiants et
   mots de passe **directement dans GitHub**. `username` est l'identifiant Agalan utilisé
   pour se connecter à LEO, qui peut être différent du nom avant `@` dans l'adresse mail.
   Conserver l'ordre correspondant à chaque adresse. Ne jamais publier le fichier rempli.
   Les secrets existants `AFF_EMAIL`, `UGA_USER`, `UGA_PASS` restent compatibles si leurs
   listes séparées par des virgules ont le même nombre d'éléments et si aucun mot de
   passe ne contient une virgule. `ACCOUNTS_JSON` prend priorité lorsqu'il est renseigné.
3. Garder `TG_TOKEN` et `TG_CHAT` si les notifications Telegram sont souhaitées.
   `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `IMAP_USER` et `IMAP_PASS` ne sont pas utilisés.
4. Actions → Réservation BU fiable → Run workflow. Choisir `diagnostic` et laisser
   la date vide. Ce mode teste les connexions, lit les mails et simule les réservations;
   il ne réserve ni ne confirme et ne sauvegarde pas le journal.
5. Quand les trois lignes « webmail accessible » et le diagnostic sont bons, lancer
   avec `execute` et la date souhaitée. `rattrapage` couvre les jours ouvrés de J à J+14.
   Code de sortie 2 / workflow rouge = couverture incomplète, même si certaines
   réservations sont bien confirmées. Le rapport détaille les durées.

## Horaires et capacité

- Objectif: lundi–vendredi, 11h30–21h, J+14.
- Priorité: RDC Sud n°4, RDC Sud n°5, autres RDC Sud, autres RDC, étage.
- 3h maximum par compte réel et par jour; aucun alias généré.
- Trois comptes permettent au maximum 9h, alors que l'objectif représente 9h30.
  La couverture complète reste donc impossible avec ces trois comptes et ce quota.
  Ajouter un quatrième participant autorisé dans le JSON, ou régler `START: "12:00"`
  (ou `END: "20:30"`) dans le workflow pour demander 9h. Le nombre de personnes
  `NB_PERSONNES` doit correspondre aux personnes réellement présentes.
- Le bot privilégie l'heure de début et la salle demandée. Il peut changer de salle
  lorsqu'une partie de la journée n'est plus disponible. Les créneaux sont de 30 minutes.
- Les réservations faites manuellement sont reprises lorsqu'elles figurent dans les
  mails Affluences accessibles. Un état incertain bloque un doublon et reste à vérifier;
  une absence de mail ne constitue jamais une preuve d'annulation.

## Déclenchement automatique

Le workflow démarre à 23h56 et 6h56, heure de Paris, puis attend respectivement minuit
et 7h avant les demandes. Un rattrapage est prévu à 15h17 en semaine. Le passage à
l'heure d'été est géré par `timezone: Europe/Paris`.

Ces heures d'ouverture ne sont pas garanties par une documentation du service:
elles reprennent les hypothèses du bot fourni. La page de l'UGA annonce jusqu'à
15 jours d'avance; `DAYS_AHEAD` reste à 14 conformément à l'objectif initial et peut
être réglé à 15 après vérification d'une date dans le diagnostic.

GitHub Actions peut démarrer en retard ou ignorer un lancement en cas de surcharge.
Il ne garantit pas une réservation exactement à la première seconde. Pour un horaire
strict, exécuter le même programme sur une machine allumée/VPS avec un ordonnanceur
système, un seul processus et un dossier `state` conservé. Aucun VPS n'a été déployé.

## Utilisation locale

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Créer localement un fichier `accounts.json` à partir de l'exemple, renseigner ses valeurs,
puis le charger sans afficher les secrets:

```sh
export ACCOUNTS_JSON="$(cat accounts.json)"
python reserver.py --diagnostic --headed
python reserver.py 2026-12-02 --execute
python reserver.py rattrapage --execute
```

Ne pas lancer les commandes `--execute` en même temps que le workflow GitHub.
Le programme ne persiste aucun mot de passe, cookie, jeton ou lien de confirmation.
Le journal contient seulement comptes, dates, salles, horaires et statuts; ne pas le
commiter. Le cache GitHub n'est pas une base durable garantie: la reconstruction par
les mails réduit ce risque. Si une réservation n'est présente ni dans les mails ni
dans le journal, le programme ne peut pas connaître son existence à l'avance.

## Tests et dépannage

```sh
python -m unittest discover -s tests -v
```

- Connexion refusée: vérifier l'identifiant **Agalan**, le mot de passe et toute étape
  supplémentaire du portail. Le bot ne contourne pas un CAPTCHA ou un second facteur.
- « session webmail non utilisable »: le serveur n'a pas accepté la session ou son
  jeton CSRF; garder le mode diagnostic et vérifier la compatibilité avec le serveur.
- « mail Affluences non interprétable »: modèle de mail différent. Arrêt avant les
  réservations pour éviter un calcul basé sur un historique incomplet.
- « uncertain »: réponse réseau perdue ou réservation déjà présente sans preuve
  exploitable. Vérifier la réservation réelle; ne pas supprimer le journal pour relancer
  à l'aveugle.
- Confirmation: seul un message explicite de réussite sur la page Affluences ou un
  mail « réservation confirmée » permet d'annoncer le succès. Une page qui s'ouvre,
  un HTTP 201 et `already_have_reservation` ne suffisent pas.
- Les confirmations d'emails en attente sont aussi recherchées lors d'un passage
  sans nouvelle réservation. Les liens d'annulation sont toujours exclus.

## Références vérifiées

- Modalités UGA (3h et jusqu'à 15 jours):
  https://bibliotheques.univ-grenoble-alpes.fr/services/reserver-des-espaces/reserver-des-espaces-191434.kjsp
- Délais possibles de GitHub Actions:
  https://docs.github.com/en/actions/how-tos/troubleshoot-workflows
- Programmation avec fuseau horaire:
  https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- Partage des cookies entre contexte Playwright et client HTTP:
  https://playwright.dev/python/docs/api/class-apirequestcontext
- Format SOAP JSON Zimbra:
  https://github.com/Zimbra/zm-mailbox/blob/develop/store/docs/soap.txt
