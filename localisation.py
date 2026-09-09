import json
import os
import sys
import time
import math
import threading
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
import requests
import ssl

FICHIER_COORDONNEES = "coordonnees.txt"   # une URL par ligne (lignes vides et # ignorées)
FICHIER_JSON = "historique_positions.json"
FICHIER_PARAM = "localisationParam.json"
# ═══════════════════════════════════════════════════════════════
#  VERSION DU SERVEUR DE LOCALISATION — à incrémenter à chaque
#  modification. Exposée par /version et /sante, affichée au
#  démarrage, et renvoyée dans /distances et /preferences pour que
#  le dashboard puisse vérifier à quelle version il parle.
# ═══════════════════════════════════════════════════════════════
VERSION_LOCALISATION = "1.1.0"
HISTORIQUE_VERSIONS = [
    ("1.1.0", "09/09/2026",
     "Les onze routes étaient comparées à self.path, chaîne de requête "
     "comprise : un appel /distances?t=123 tombait dans le 404 muet. La "
     "comparaison porte désormais sur le chemin seul. Ajout : routes /version "
     "et /sante, 404 explicite listant les routes disponibles, 500 tracé au "
     "lieu d'une connexion coupée, en-tête no-store sur les réponses JSON, "
     "bannière de démarrage annonçant les vraies adresses (HTTP sur 127.0.0.1 "
     "uniquement, HTTPS sur 8283), arrêt propre libérant les ports, options "
     "--foreground et --version."),
    ("1.0.0", "avant 09/09/2026",
     "Version initiale : lecture périodique des positions GPS depuis GitHub, "
     "historique JSON, distances, périodes, préférences, nettoyage, saisie "
     "manuelle, serveurs HTTP (8282) et HTTPS Tailscale (8283) multi-threads."),
]

DEMARRAGE_TS = time.time()

PORT_SERVEUR = 8282
PERIOD_DEFAUT_MIN = 60

# [AJOUT] Plafond du fichier d'historique JSON, pour éviter une croissance illimitée
# entre deux nettoyages manuels (/nettoyer). 20000 entrées ≈ plusieurs années à la
# fréquence par défaut (2 personnes, lecture toutes les 60 min) — largement suffisant
# pour le graphique d'absences sur 42 jours, qui n'a besoin que de quelques milliers
# d'entrées récentes.
HISTORIQUE_MAX_ENTREES = 20000

# [AJOUT] Verrous protégeant les fichiers JSON partagés contre les écritures
# concurrentes. Depuis le passage à ThreadingHTTPServer, plusieurs requêtes
# (dashboard, paramétrage, diagnostic, saisie manuelle, lecture GitHub
# périodique...) peuvent désormais s'exécuter en parallèle sur des threads
# différents. Sans verrou, un cycle lecture → modification → écriture qui
# chevauche celui d'une autre requête peut écraser silencieusement la
# modification de l'autre ("lost update").
_PARAM_LOCK     = threading.RLock()   # protège FICHIER_PARAM (localisationParam.json)
_POSITIONS_LOCK = threading.Lock()    # protège FICHIER_JSON (historique_positions.json)


def _ecrire_json_atomique(chemin, donnees):
    """
    Écrit `donnees` en JSON dans `chemin` de façon atomique (fichier temporaire
    puis os.replace). Empêche un lecteur concurrent — en particulier la route
    statique qui sert historique_positions.json directement depuis le disque,
    et qui ne passe donc par aucun verrou Python — de tomber sur un fichier
    tronqué pendant qu'une écriture est en cours.
    """
    tmp = f"{chemin}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=4)
    os.replace(tmp, chemin)


# ─────────────────────────────────────────────
#  Lecture des URLs depuis coordonnees.txt
# ─────────────────────────────────────────────

def lire_urls_coordonnees():
    """
    Lit toutes les URLs valides de FICHIER_COORDONNEES.
    Retourne une liste de chaînes (peut être vide).
    Les lignes vides et celles commençant par '#' sont ignorées.
    """
    if not os.path.exists(FICHIER_COORDONNEES):
        print(f"[AVERTISSEMENT] Fichier '{FICHIER_COORDONNEES}' introuvable — aucune URL chargée.")
        return []
    with open(FICHIER_COORDONNEES, "r", encoding="utf-8") as f:
        lignes = f.readlines()
    urls = [l.strip() for l in lignes if l.strip() and not l.strip().startswith("#")]
    # Convertir automatiquement les URLs github.com/blob/ en raw.githubusercontent.com
    urls_converties = []
    for u in urls:
        if "github.com" in u and "/blob/" in u:
            u = (u.replace("github.com", "raw.githubusercontent.com")
                  .replace("/blob/", "/"))
            print(f"[AVERTISSEMENT] URL GitHub convertie en raw : {u}")
        urls_converties.append(u)
    return urls_converties


def url_active():
    """Retourne la première URL du fichier coordonnees.txt, ou None si vide."""
    urls = lire_urls_coordonnees()
    return urls[0] if urls else None


# ─────────────────────────────────────────────
#  Géographie
# ─────────────────────────────────────────────

def calculer_distance_m(lat1, lon1, lat2, lon2):
    """Distance en mètres entre deux coordonnées (Haversine)."""
    R = 6_371_000
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def extraire_coordonnees(entree):
    """Retourne (lat, lon) ou None si absent/invalide."""
    try:
        if "latitude" in entree and "longitude" in entree:
            return float(entree["latitude"]), float(entree["longitude"])
        if "position" in entree and "," in entree["position"]:
            lat, lon = entree["position"].split(",", 1)
            return float(lat), float(lon)
    except (ValueError, TypeError):
        pass
    return None


# ─────────────────────────────────────────────
#  Nettoyage du JSON
# ─────────────────────────────────────────────

def nettoyer_historique(seuil_m=500):
    """
    Supprime les enregistrements dont la position est à moins de `seuil_m` mètres
    du point immédiatement précédent (dans l'ordre chronologique).
    Retourne (nb_avant, nb_apres).
    """
    # [FIX] Verrou : ce cycle lecture → filtrage → écriture ne doit pas
    # chevaucher un enregistrer_dans_json() concurrent, sous peine de perdre
    # une position fraîchement reçue pendant le nettoyage.
    with _POSITIONS_LOCK:
        if not os.path.exists(FICHIER_JSON):
            return 0, 0

        with open(FICHIER_JSON, "r", encoding="utf-8") as f:
            historique = json.load(f)

        if not isinstance(historique, list):
            return 0, 0

        nb_avant = len(historique)
        conserves = []
        dernieres_coords = {}   # { nomfichier: (lat, lon) } — une référence par personne

        for entree in historique:
            coords = extraire_coordonnees(entree)
            if coords is None:
                # Pas de GPS : on conserve sans filtrer
                conserves.append(entree)
                continue

            cle = entree.get("nomfichier", "_inconnu")

            if cle not in dernieres_coords:
                # Premier point connu pour cette personne : toujours conservé
                conserves.append(entree)
                dernieres_coords[cle] = coords
            else:
                dist = calculer_distance_m(*dernieres_coords[cle], *coords)
                if dist >= seuil_m:
                    conserves.append(entree)
                    dernieres_coords[cle] = coords
                # sinon : trop proche du dernier point de CETTE personne → supprimé

        _ecrire_json_atomique(FICHIER_JSON, conserves)

        return nb_avant, len(conserves)


# ─────────────────────────────────────────────
#  Paramètres (localisationParam.json)
# ─────────────────────────────────────────────

DISTANCES_DEFAUT = {
    "maison_lat":        48.83389,  # latitude du point de référence "laMaison"
    "maison_lon":        2.29546,   # longitude du point de référence "laMaison"
    "maison_rayon_km":   1.0,       # rayon (km) : en-dessous → personne "à la maison"
    "nettoyage_seuil_m": 500,       # seuil (m) : nettoyerProches supprime les points < seuil
    "carte_filtre_km":   1.0,       # seuil (km) : filtre d'affichage des points historiques
}


def charger_parametres():
    """
    Charge les paramètres depuis FICHIER_PARAM.
    Si le fichier n'existe pas, le crée avec les valeurs par défaut.
    Retourne le dict des paramètres.
    """
    # [FIX] Verrou : plusieurs requêtes (dashboard, paramétrage, diagnostic,
    # cameraServer.py via /preferences...) peuvent appeler charger/sauvegarder_*
    # en parallèle depuis le passage à ThreadingHTTPServer. Sans verrou, un
    # cycle lecture → modification → écriture qui chevauche celui d'une autre
    # requête peut écraser silencieusement la modification de l'autre.
    with _PARAM_LOCK:
        if not os.path.exists(FICHIER_PARAM):
            parametres = {"period": PERIOD_DEFAUT_MIN, "distances": DISTANCES_DEFAUT.copy()}
            _ecrire_json_atomique(FICHIER_PARAM, parametres)
            return parametres

        try:
            with open(FICHIER_PARAM, "r", encoding="utf-8") as f:
                parametres = json.load(f)
            if not isinstance(parametres, dict):
                parametres = {}
        except (json.JSONDecodeError, OSError):
            parametres = {}

        modifie = False
        if "period" not in parametres:
            parametres["period"] = PERIOD_DEFAUT_MIN
            modifie = True

        # Assurer la présence de la clé "distances" avec toutes ses sous-clés
        if "distances" not in parametres or not isinstance(parametres["distances"], dict):
            parametres["distances"] = DISTANCES_DEFAUT.copy()
            modifie = True
        else:
            for cle, valeur_defaut in DISTANCES_DEFAUT.items():
                if cle not in parametres["distances"]:
                    parametres["distances"][cle] = valeur_defaut
                    modifie = True

        if modifie:
            _ecrire_json_atomique(FICHIER_PARAM, parametres)

        return parametres


def sauvegarder_periode(nouvelle_periode):
    """
    Met à jour la valeur "period" (en minutes) dans FICHIER_PARAM et la sauvegarde.
    Retourne le dict des paramètres mis à jour.
    """
    with _PARAM_LOCK:
        parametres = charger_parametres()
        parametres["period"] = nouvelle_periode
        _ecrire_json_atomique(FICHIER_PARAM, parametres)
        return parametres


def charger_distances():
    """Retourne le dict parametres['distances'] avec valeurs de repli sur DISTANCES_DEFAUT."""
    parametres = charger_parametres()
    d = parametres.get("distances", {})
    return {cle: d.get(cle, DISTANCES_DEFAUT[cle]) for cle in DISTANCES_DEFAUT}


def sauvegarder_distances(nouvelles_valeurs):
    """
    Fusionne nouvelles_valeurs dans parametres['distances'] et sauvegarde.
    Retourne le dict distances mis à jour.
    """
    with _PARAM_LOCK:
        parametres = charger_parametres()
        distances = parametres.get("distances", {})
        if not isinstance(distances, dict):
            distances = {}
        # Valider et convertir chaque valeur reçue
        for cle in DISTANCES_DEFAUT:
            if cle in nouvelles_valeurs:
                try:
                    distances[cle] = float(nouvelles_valeurs[cle])
                except (TypeError, ValueError):
                    pass  # on garde l'ancienne valeur
        parametres["distances"] = distances
        _ecrire_json_atomique(FICHIER_PARAM, parametres)
        return distances


# ── [AJOUT] ── Préférences génériques du dashboard HTML ─────────────
# Stockées dans le même FICHIER_PARAM, sous la clé "dashboard".
# Permet de remplacer le localStorage (propre à un appareil) par une
# persistance partagée côté serveur, restaurée sur n'importe quel appareil.

def charger_preferences_dashboard():
    """
    Retourne le dict parametres["dashboard"] (vide si absent).
    """
    parametres = charger_parametres()
    dashboard = parametres.get("dashboard")
    if not isinstance(dashboard, dict):
        dashboard = {}
    return dashboard


def sauvegarder_preferences_dashboard(nouvelles_valeurs):
    """
    Fusionne nouvelles_valeurs dans parametres["dashboard"] et sauvegarde.
    Retourne le dict dashboard mis à jour.
    """
    # [FIX] C'est la fonction la plus sollicitée en concurrence (checkboxes du
    # dashboard, cycles de diagnostic, synchronisation depuis cameraServer.py...)
    # — le verrou est indispensable ici pour ne pas perdre de préférences.
    with _PARAM_LOCK:
        parametres = charger_parametres()
        dashboard = parametres.get("dashboard")
        if not isinstance(dashboard, dict):
            dashboard = {}
        dashboard.update(nouvelles_valeurs)
        parametres["dashboard"] = dashboard
        _ecrire_json_atomique(FICHIER_PARAM, parametres)
        return dashboard
# ── [FIN AJOUT] ──────────────────────────────────────────────────────


# Paramètres chargés au démarrage (initialisés dans __main__ après os.chdir,
# et actualisés ensuite via la route /periode)
PARAMETRES = {"period": PERIOD_DEFAUT_MIN}

# ── [AJOUT] ── Lecture GPS forcée depuis le dashboard ───────────────────────
# evenement_lecture_demandee : réveille immédiatement la boucle principale
#   (qui attend normalement `period` minutes) pour déclencher une lecture
#   tout de suite, SANS lecture en double : c'est toujours la boucle
#   principale qui appelle lire_statut_web(), jamais le handler HTTP.
#   Une fois la lecture terminée, la boucle recompte un cycle complet de
#   `period` minutes → le délai est donc bien réinitialisé.
# evenement_lecture_terminee : permet au handler /actualiser de répondre au
#   dashboard une fois la lecture forcée effectivement terminée (avec un
#   timeout de sécurité pour ne jamais bloquer indéfiniment la requête HTTP).
evenement_lecture_demandee = threading.Event()
evenement_lecture_terminee = threading.Event()
# ── [FIN AJOUT] ──────────────────────────────────────────────────────────




# ── [AJOUT MODE SERVEUR] ── Retourne la dernière position connue depuis le JSON ──
def _derniere_position():
    """Lit la dernière entrée du JSON et retourne lat/lon si disponible."""
    if not os.path.exists(FICHIER_JSON):
        return None
    try:
        with open(FICHIER_JSON, "r", encoding="utf-8") as f:
            historique = json.load(f)
        if not isinstance(historique, list) or not historique:
            return None
        # Parcourir en sens inverse pour trouver la dernière avec coordonnées
        for entree in reversed(historique):
            coords = extraire_coordonnees(entree)
            if coords:
                lat, lon = coords
                return {
                    "ok": True,
                    "latitude": lat,
                    "longitude": lon,
                    "timestamp": entree.get("timestamp_enregistrement", ""),
                }
    except Exception:
        pass
    return None
# ── [FIN AJOUT] ────────────────────────────────────────────────────────────────

class Gestionnaire(SimpleHTTPRequestHandler):

    def do_OPTIONS(self):
        """Répondre aux preflight CORS (navigateur cross-origin)."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        """Enveloppe : une exception non rattrapée coupait la connexion
        (ERR_EMPTY_RESPONSE côté navigateur, 502 opaque côté proxy), sans
        rien laisser d'exploitable. On renvoie désormais un 500 tracé."""
        try:
            self._get_interne()
        except BrokenPipeError:
            pass
        except Exception as e:
            self.envoyer_500(e)

    def _get_interne(self):
        """Servir les fichiers statiques avec le header CORS.

        [v1.1.0] chemin = self.path SANS la chaîne de requête. Toutes les
        comparaisons portent dessus : « /distances?t=123 » ne correspondait
        à aucune route et retombait dans le 404 muet, ce qui faisait croire
        que le processus était absent."""
        chemin = self.path.split("?")[0]

        if chemin == "/version":
            self.renvoyer_json(json.dumps({
                "version": VERSION_LOCALISATION,
                "historique": [{"version": v, "date": d, "resume": r}
                               for v, d, r in HISTORIQUE_VERSIONS],
            }).encode("utf-8"))
            return

        if chemin == "/sante":
            self.renvoyer_json(json.dumps(self._sante(), ensure_ascii=False).encode("utf-8"))
            return

        # ── [AJOUT MODE SERVEUR] ── Route /position pour cameraServer.py ──
        if chemin == "/position":
            derniere = _derniere_position()
            if derniere:
                corps = json.dumps(derniere).encode("utf-8")
            else:
                corps = json.dumps({"ok": False, "raison": "aucune_position"}).encode("utf-8")
            self.renvoyer_json(corps)
            return

        if chemin == "/coordonnees":
            urls = lire_urls_coordonnees()
            corps = json.dumps({
                "ok": True,
                "urls": urls,
                "url_active": urls[0] if urls else None,
            }).encode("utf-8")
            self.renvoyer_json(corps)
            return

        if chemin == "/periode":
            corps = json.dumps({
                "ok": True,
                "period": PARAMETRES.get("period", PERIOD_DEFAUT_MIN),
            }).encode("utf-8")
            self.renvoyer_json(corps)
            return

        # ── Route /distances ──────────────────────────────────────────────
        if chemin == "/distances":
            distances = charger_distances()
            corps = json.dumps({"ok": True, "distances": distances,
                                "version_localisation": VERSION_LOCALISATION}).encode("utf-8")
            self.renvoyer_json(corps)
            return

        # ── [AJOUT] ── Préférences génériques du dashboard HTML ──
        if chemin == "/preferences":
            dashboard = charger_preferences_dashboard()
            corps = json.dumps({
                "ok": True,
                "preferences": dashboard,
            }).encode("utf-8")
            self.renvoyer_json(corps)
            return
        # ── [FIN AJOUT] ──
        # ── [FIN AJOUT] ────────────────────────────────────────────────────
        # Fichier statique : on vérifie son existence pour renvoyer un 404 JSON
        # exploitable plutôt que la page HTML de SimpleHTTPRequestHandler, que
        # le proxy et le dashboard ne savent pas interpréter.
        cible = self.translate_path(self.path)
        if not (os.path.isfile(cible) or os.path.isdir(cible)):
            self.envoyer_404(f"aucune route ni fichier « {chemin} »")
            return
        super().do_GET()
        # Note : SimpleHTTPRequestHandler écrit les headers lui-même ;
        # on ne peut pas les injecter après coup via super().
        # On surcharge end_headers() à la place (voir ci-dessous).

    def end_headers(self):
        """Injecter Access-Control-Allow-Origin sur toutes les réponses."""
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_POST(self):
        try:
            self._post_interne()
        except BrokenPipeError:
            pass
        except Exception as e:
            self.envoyer_500(e)

    def _post_interne(self):
        chemin = self.path.split("?")[0]
        if chemin == "/nettoyer":
            seuil = int(charger_distances().get("nettoyage_seuil_m", 500))
            nb_avant, nb_apres = nettoyer_historique(seuil_m=seuil)
            supprimes = nb_avant - nb_apres
            corps = json.dumps({
                "ok": True,
                "nb_avant": nb_avant,
                "nb_apres": nb_apres,
                "supprimes": supprimes
            }).encode("utf-8")
            self.renvoyer_json(corps)
            print(f"[Nettoyage] {supprimes} enregistrement(s) supprimé(s) ({nb_avant} → {nb_apres}) — seuil={seuil} m")
            
        elif chemin == "/actualiser":
            # [MODIFIÉ] On ne lit plus GitHub directement depuis ce thread HTTP :
            # on réveille la boucle principale (celle qui gère le délai `period`)
            # pour qu'elle fasse la lecture elle-même. Cela évite toute lecture
            # en double et garantit que le délai avant la prochaine lecture
            # automatique est bien réinitialisé à `period` minutes à partir de
            # cette lecture forcée.
            print("[Serveur] Demande d'actualisation manuelle reçue. Réveil de la boucle de lecture GPS...")
            evenement_lecture_terminee.clear()
            evenement_lecture_demandee.set()

            # On attend (avec un timeout de sécurité) que la boucle principale
            # ait fini sa lecture, pour renvoyer une réponse cohérente au dashboard.
            # [NOTE] Le proxy /loc/ de cameraServer.py a lui-même un timeout de 10 s
            # (urllib.request.urlopen(..., timeout=10)) : on reste donc prudemment
            # en dessous pour renvoyer une réponse avant que le proxy ne coupe.
            termine = evenement_lecture_terminee.wait(timeout=8)
            if termine:
                message = "Données actualisées depuis GitHub — délai réinitialisé"
            else:
                message = "Actualisation lancée (en cours) — délai réinitialisé"
            corps = json.dumps({"ok": True, "message": message}).encode("utf-8")
            self.renvoyer_json(corps)
        
        elif chemin == "/saisie_manuelle":
            longueur = int(self.headers.get('Content-Length', 0))
            corps_brut = self.rfile.read(longueur)
            try:
                entree = json.loads(corps_brut.decode('utf-8'))
                enregistrer_dans_json(entree)
                print(f"[Saisie manuelle] {entree.get('nomfichier','?')} — {entree.get('latitude','?')},{entree.get('longitude','?')}")
                rep = json.dumps({"ok": True}).encode("utf-8")
            except Exception as e:
                rep = json.dumps({"ok": False, "erreur": str(e)}).encode("utf-8")
            self.renvoyer_json(rep)
        
        elif chemin == "/periode":
            longueur = int(self.headers.get('Content-Length', 0))
            corps_brut = self.rfile.read(longueur)
            print(f"[Période] Requête POST reçue ({longueur} octet(s)) : {corps_brut!r}")
            try:
                donnees = json.loads(corps_brut.decode('utf-8'))
                nouvelle_periode = float(donnees.get("period"))
                if nouvelle_periode <= 0:
                    raise ValueError("La période doit être positive")
                global PARAMETRES
                PARAMETRES = sauvegarder_periode(nouvelle_periode)
                print(f"[Paramètres] Période modifiée → {nouvelle_periode} minute(s)")
                rep = json.dumps({"ok": True, "period": PARAMETRES["period"]}).encode("utf-8")
            except Exception as e:
                print(f"[Période] Erreur lors de la mise à jour : {e!r}")
                rep = json.dumps({"ok": False, "erreur": str(e)}).encode("utf-8")
            self.renvoyer_json(rep)

        # ── [AJOUT] ── Préférences génériques du dashboard HTML ──
        elif chemin == "/preferences":
            longueur = int(self.headers.get('Content-Length', 0))
            corps_brut = self.rfile.read(longueur)
            try:
                donnees = json.loads(corps_brut.decode('utf-8'))
                if not isinstance(donnees, dict):
                    raise ValueError("Le corps doit être un objet JSON")
                dashboard = sauvegarder_preferences_dashboard(donnees)
                rep = json.dumps({"ok": True, "preferences": dashboard}).encode("utf-8")
            except Exception as e:
                print(f"[Préférences] Erreur lors de la mise à jour : {e!r}")
                rep = json.dumps({"ok": False, "erreur": str(e)}).encode("utf-8")
            self.renvoyer_json(rep)
        # ── [FIN AJOUT] ──

        # ── Route /distances ──────────────────────────────────────────────
        elif chemin == "/distances":
            longueur = int(self.headers.get('Content-Length', 0))
            corps_brut = self.rfile.read(longueur)
            try:
                donnees = json.loads(corps_brut.decode('utf-8'))
                if not isinstance(donnees, dict):
                    raise ValueError("Le corps doit être un objet JSON")
                distances = sauvegarder_distances(donnees)
                print(f"[Distances] Paramètres mis à jour : {distances}")
                rep = json.dumps({"ok": True, "distances": distances}).encode("utf-8")
            except Exception as e:
                print(f"[Distances] Erreur lors de la mise à jour : {e!r}")
                rep = json.dumps({"ok": False, "erreur": str(e)}).encode("utf-8")
            self.renvoyer_json(rep)

        else:
            self.envoyer_404()

    # ═══════════════════════════════════════════════════════════
    #  Réponses utilitaires
    # ═══════════════════════════════════════════════════════════
    ROUTES_CONNUES = {
        "GET":  ["/position", "/coordonnees", "/periode", "/distances",
                 "/preferences", "/version", "/sante", "/<fichier statique>"],
        "POST": ["/nettoyer", "/actualiser", "/saisie_manuelle", "/periode",
                 "/preferences", "/distances"],
    }

    def renvoyer_json(self, corps, code=200):
        """Entêtes centralisés des réponses JSON.
        [v1.1.0] no-store : sans cela un navigateur peut resservir d'anciens
        paramètres après une modification."""
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Localisation-Version", VERSION_LOCALISATION)
        # Access-Control-Allow-Origin ajouté automatiquement par end_headers()
        self.end_headers()
        self.wfile.write(corps)

    def envoyer_404(self, detail=""):
        """404 avec un corps : un 404 muet ne dit pas si le processus est
        absent ou si c'est la route qui n'existe pas."""
        print(f"[HTTP] ⚠️  404 {self.command} {self.path} — {detail or 'route inconnue'}")
        self.renvoyer_json(json.dumps({
            "ok": False,
            "erreur": "route inconnue",
            "detail": detail,
            "methode": self.command,
            "chemin": self.path,
            "routes_disponibles": self.ROUTES_CONNUES.get(self.command, []),
            "version_localisation": VERSION_LOCALISATION,
        }, ensure_ascii=False).encode("utf-8"), 404)

    def envoyer_500(self, exc):
        import traceback
        print(f"[HTTP] ❌ Exception sur {self.command} {self.path} : {exc!r}")
        print(traceback.format_exc())
        try:
            self.renvoyer_json(json.dumps({
                "ok": False,
                "erreur": str(exc),
                "chemin": self.path,
                "version_localisation": VERSION_LOCALISATION,
            }, ensure_ascii=False).encode("utf-8"), 500)
        except Exception:
            pass   # connexion déjà fermée

    def _sante(self):
        """Bilan complet en une requête, pendant de /sante côté 8585."""
        dossier = os.path.dirname(os.path.abspath(__file__))

        nb_releves, derniere = 0, None
        try:
            with open(os.path.join(dossier, FICHIER_JSON), encoding="utf-8") as f:
                entrees = json.load(f)
            if isinstance(entrees, list):
                nb_releves = len(entrees)
                if entrees:
                    derniere = entrees[-1].get("date_enregistrement")
        except Exception:
            pass

        fichiers = {}
        for nom in (FICHIER_PARAM, FICHIER_JSON, FICHIER_COORDONNEES, "localisation.log"):
            chemin_f = os.path.join(dossier, nom)
            existe = os.path.exists(chemin_f)
            fichiers[nom] = {"present": existe,
                             "octets": (os.path.getsize(chemin_f) if existe else 0)}

        uptime = int(time.time() - DEMARRAGE_TS)
        return {
            "ok": True,
            "version_localisation": VERSION_LOCALISATION,
            "pid": os.getpid(),
            "port_http": PORT_SERVEUR,
            "port_https": PORT_SERVEUR_HTTPS,
            "https_actif": HTTPS_ACTIF,
            "ecoute_http": "127.0.0.1 uniquement",
            "uptime_s": uptime,
            "uptime_lisible": f"{uptime // 3600} h {(uptime % 3600) // 60} min {uptime % 60} s",
            "dossier": dossier,
            "periode_min": PARAMETRES.get("period", PERIOD_DEFAUT_MIN),
            "urls_coordonnees": lire_urls_coordonnees(),
            "distances": charger_distances(),
            "releves": nb_releves,
            "dernier_releve": derniere,
            "threads_actifs": threading.active_count(),
            "fichiers": fichiers,
        }


PORT_SERVEUR_HTTPS = 8283   # port HTTPS accessible via Tailscale

# Certificat Tailscale : testé une seule fois, pour que la bannière de
# démarrage annonce le bon schéma et le bon port.
CERT_FILE = "/var/db/tailscale/imactavernier-2.tail78c299.ts.net.crt"
KEY_FILE  = "/var/db/tailscale/imactavernier-2.tail78c299.ts.net.key"
HTTPS_ACTIF = os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)

SERVEURS = []   # serveurs à fermer proprement à l'arrêt

def demarrer_serveur():
    # [FIX] ThreadingHTTPServer au lieu de HTTPServer : évite qu'une requête lente
    # (ex: /actualiser, qui interroge GitHub avec un timeout de 10s par URL) ne bloque
    # tout le reste (/position, /coordonnees, /distances...) pendant toute sa durée.
    # HTTP : localhost uniquement → cameraServer.py peut l'appeler, l'extérieur non
    serveur_http = ThreadingHTTPServer(("127.0.0.1", PORT_SERVEUR), Gestionnaire)
    threading.Thread(target=serveur_http.serve_forever, daemon=True).start()
    print(f"🌐 HTTP localisation v{VERSION_LOCALISATION} démarré sur "
          f"127.0.0.1:{PORT_SERVEUR} (localhost uniquement, multi-thread)")

    # ── Arrêt propre : sans cela, les ports restaient occupés quelques
    #    secondes après un kill et la relance échouait. ──
    import signal
    SERVEURS.append(serveur_http)

    def _arret(signum, frame):
        print(f"🛑 Signal {signum} reçu — arrêt des serveurs de localisation")
        for s in SERVEURS:
            threading.Thread(target=s.shutdown, daemon=True).start()
        time.sleep(0.5)
        for s in SERVEURS:
            try:
                s.server_close()
            except Exception:
                pass
        print("✅ Ports 8282/8283 libérés")
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _arret)
        except (ValueError, OSError):
            pass

    # HTTPS : toutes interfaces → accessible via Tailscale depuis l'extérieur
    if HTTPS_ACTIF:
        serveur_https = ThreadingHTTPServer(("0.0.0.0", PORT_SERVEUR_HTTPS), Gestionnaire)
        SERVEURS.append(serveur_https)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        serveur_https.socket = ctx.wrap_socket(serveur_https.socket, server_side=True)
        print(f"🔒 HTTPS localisation démarré sur le port {PORT_SERVEUR_HTTPS} (multi-thread)")
        serveur_https.serve_forever()
    else:
        print(f"⚠️ Certificat Tailscale non trouvé — HTTPS localisation désactivé")
        threading.Event().wait()


# ─────────────────────────────────────────────
#  Lecture / enregistrement de la position
# ─────────────────────────────────────────────

def analyser_contenu_brut(texte_brut):
    champs = {}
    lignes = texte_brut.strip().split("\n")
    for ligne in lignes:
        if ":" in ligne:
            clef, valeur = ligne.split(":", 1)
            clef_propre = (
                clef.strip()
                .lower()
                .replace(" ", "_")
                .replace("é", "e")
                .replace("à", "a")
            )
            champs[clef_propre] = valeur.strip()
    if not champs:
        champs["contenu_brut"] = texte_brut.strip()
    return champs


def enregistrer_dans_json(champs_extraits):
    nouvelle_entree = {
        "timestamp_enregistrement": datetime.now().isoformat(),
        "date_enregistrement": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **champs_extraits,
    }
    # [FIX] Verrou : appelée à la fois depuis la boucle périodique GitHub
    # (thread principal) et depuis /saisie_manuelle (thread de requête HTTP) —
    # sans verrou, deux écritures qui se chevauchent peuvent en perdre une.
    with _POSITIONS_LOCK:
        if os.path.exists(FICHIER_JSON):
            try:
                with open(FICHIER_JSON, "r", encoding="utf-8") as f:
                    historique = json.load(f)
                    if not isinstance(historique, list):
                        historique = []
            except json.JSONDecodeError:
                historique = []
        else:
            historique = []

        historique.append(nouvelle_entree)

        # [AJOUT] Plafonner le fichier pour éviter une croissance illimitée
        # (le nettoyage par proximité via /nettoyer reste la méthode principale ;
        # ceci n'est qu'un filet de sécurité si le nettoyage n'est jamais lancé)
        if len(historique) > HISTORIQUE_MAX_ENTREES:
            historique = historique[-HISTORIQUE_MAX_ENTREES:]

        _ecrire_json_atomique(FICHIER_JSON, historique)


def lire_statut_web():

    heure_actuelle = datetime.now().strftime("%H:%M:%S")
    urls = lire_urls_coordonnees()
    if not urls:
        print(f"[{heure_actuelle}] Aucune URL dans '{FICHIER_COORDONNEES}' — lecture ignorée.")
        return
    for url in urls:
        try:
            nom_fichier = url.split("/")[-1]   # ex: "iphoneHenri.txt"
            reponse = requests.get(url, params={"t": int(time.time())}, timeout=10)
            reponse.raise_for_status()

            texte_brut = reponse.text
            champs_extraits = analyser_contenu_brut(texte_brut)
            champs_extraits["nomfichier"] = nom_fichier   # ← ajout du nom de fichier
            enregistrer_dans_json(champs_extraits)



            position = ""
            if "latitude" in champs_extraits and "longitude" in champs_extraits:
                position = f" | Pos: {champs_extraits['latitude']},{champs_extraits['longitude']}"
            elif "position" in champs_extraits:
                position = f" | Pos: {champs_extraits['position']}"

            batterie = (
                f" | Bat: {champs_extraits['batterie']}"
                if "batterie" in champs_extraits else ""
            )
            statut = (
                f" | Statut: {champs_extraits['statut']}"
                if "statut" in champs_extraits else ""
            )

            print(f"[{heure_actuelle}] [{nom_fichier}] Données lues et enregistrées{position}{batterie}{statut}")

        except requests.exceptions.RequestException as e:
            print(f"[{heure_actuelle}] [{nom_fichier}] Erreur de communication : {e}")

# ─────────────────────────────────────────────
#  Point d'entrée
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # ── Options de ligne de commande ─────────────────────────────
    #   --version      affiche la version et quitte
    #   --foreground   reste au premier plan (logs dans le terminal,
    #                  Ctrl-C pour arrêter) : indispensable pour déboguer
    _args = sys.argv[1:]
    if "--version" in _args or "-v" in _args:
        print(f"localisation.py v{VERSION_LOCALISATION}")
        for _v, _d, _r in HISTORIQUE_VERSIONS:
            print(f"  v{_v} ({_d}) — {_r}")
        raise SystemExit(0)
    if "--aide" in _args or "--help" in _args or "-h" in _args:
        print("Usage : python3.13 localisation.py [--foreground] [--version]")
        raise SystemExit(0)
    PREMIER_PLAN = "--foreground" in _args or "--fg" in _args

    # Le script se place lui-même dans son propre répertoire
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # Charger (ou créer) le fichier de paramètres (localisationParam.json)
    PARAMETRES = charger_parametres()

    fichier_log = os.path.join(script_dir, "localisation.log")

    # Charger les URLs pour l'affichage de démarrage
    urls_demarrage = lire_urls_coordonnees()
    url_demarrage  = urls_demarrage[0] if urls_demarrage else "(aucune URL trouvée)"

    # ── Affichage sur le terminal AVANT toute redirection ──
    print("─" * 60)
    print(f"  localisation.py v{VERSION_LOCALISATION}")
    print(f"  Fichier de log JSON  : {os.path.join(script_dir, FICHIER_JSON)}")
    print(f"  Log du script        : {fichier_log}")
    print(f"  API HTTP             : http://127.0.0.1:{PORT_SERVEUR}/  "
          f"(localhost uniquement — inaccessible depuis le réseau)")
    if HTTPS_ACTIF:
        print(f"  API HTTPS (Tailscale): https://imactavernier-2.tail78c299.ts.net:{PORT_SERVEUR_HTTPS}/")
    else:
        print(f"  API HTTPS            : désactivée (certificat Tailscale absent)")
    print(f"  Bilan de santé       : http://127.0.0.1:{PORT_SERVEUR}/sante")
    print(f"  Vu du dashboard      : via le proxy /loc/* de cameraServer.py (port 8585)")
    print(f"  Visualiseur HTML     : http://127.0.0.1:{PORT_SERVEUR}/visualiseur_logs.html")
    print(f"  Fichier coordonnées  : {os.path.join(script_dir, FICHIER_COORDONNEES)}")
    print(f"  Fichier paramètres   : {os.path.join(script_dir, FICHIER_PARAM)}")
    print(f"  Période de lecture   : {PARAMETRES.get('period', PERIOD_DEFAUT_MIN)} minute(s)")
    print(f"  URL active           : {url_demarrage}")
    if len(urls_demarrage) > 1:
        for i, u in enumerate(urls_demarrage[1:], start=2):
            print(f"  URL #{i} (future)      : {u}")
    print("─" * 60)
    print("  Démarrage en arrière-plan, vous pouvez fermer ce terminal.")
    print("─" * 60)

    # ── Détachement du terminal (fork), sauf en --foreground ──
    if PREMIER_PLAN:
        print(f"  PREMIER PLAN (PID : {os.getpid()}) — Ctrl-C pour arrêter.")
        print("─" * 60)
    else:
        pid = os.fork()
        if pid > 0:
            # Processus parent : affiche le PID et se termine proprement
            print(f"  Script lancé (PID : {pid}).")
            print(f"  Arrêt : kill {pid}   ·   ou : pkill -f \"localisation.py\"")
            print("─" * 60)
            raise SystemExit(0)

        # ── Processus enfant : redirection des sorties vers le fichier log ──
        sys.stdout.flush()
        sys.stderr.flush()
        log_fd = open(fichier_log, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_fd
        sys.stderr = log_fd

        # Détachement complet de la session terminal
        os.setsid()

    print(f"\n{'─' * 60}")
    print(f"  Démarré le {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─' * 60}")

    thread_serveur = threading.Thread(target=demarrer_serveur, daemon=True)
    thread_serveur.start()

    while True:
        try:
            lire_statut_web()
        except Exception as e:
            print(f"[Erreur inattendue] {e}")
        finally:
            # Signale au handler /actualiser (s'il attend) que la lecture est terminée.
            evenement_lecture_terminee.set()

        periode_minutes = PARAMETRES.get("period", PERIOD_DEFAUT_MIN)
        # [MODIFIÉ] On remplace time.sleep() par un Event.wait() interruptible :
        # - si personne ne déclenche de lecture forcée, on attend simplement
        #   `periode_minutes` minutes comme avant ;
        # - si /actualiser est appelé entre-temps, l'événement est levé et
        #   wait() retourne immédiatement → la boucle relance aussitôt une
        #   lecture, puis recompte un cycle complet de `periode_minutes`
        #   minutes : le délai est donc bien réinitialisé.
        evenement_lecture_demandee.clear()
        reveil_force = evenement_lecture_demandee.wait(timeout=periode_minutes * 60)
        if reveil_force:
            print("[Cycle] Lecture forcée demandée depuis le dashboard — délai réinitialisé.")

