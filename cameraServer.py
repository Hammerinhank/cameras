"""
cameraServer.py — Serveur HTTP port 8585 + contrôle caméras Nest via Selenium

Lancement (simple) :
    python3.13 cameraServer.py

Le script affiche l'URL puis se met automatiquement en arrière-plan.
Le terminal est libre immédiatement.
Les logs sont visibles dans la page HTML à http://localhost:8585
"""

import os
import sys
import time
import json
import threading
import collections
import queue
import requests
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.keys import Keys

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
#  VERSION DU SERVEUR — à incrémenter à chaque modification.
#  Exposée par /version et /sante, affichée au démarrage et renvoyée
#  dans /status (le dashboard peut ainsi vérifier qu'il parle bien
#  à la version de serveur qu'il attend).
# ═══════════════════════════════════════════════════════════════
VERSION_SERVEUR = "1.1.0"
HISTORIQUE_VERSIONS = [
    ("1.1.0", "09/09/2026",
     "Dashboard renommé cameras.html (CameraOnOff.html reste accepté) ; "
     "service statique généralisé au dossier du script ; en-têtes CORS et "
     "no-store sur toutes les réponses ; 404 explicite listant les routes ; "
     "toute exception d'un handler renvoie un 500 JSON tracé au lieu de "
     "couper la connexion ; routes /version et /sante ; bannière de démarrage "
     "affichant le bon schéma (https quand le certificat Tailscale est là) ; "
     "arrêt propre sur SIGTERM/SIGINT ; options --foreground, --port, --version."),
    ("1.0.0", "avant 09/09/2026",
     "Version initiale : contrôle Selenium des caméras Nest, file d'attente de "
     "basculement, diagnostic par caméra, modes proximité et actualisation, "
     "proxy /loc/* vers localisation.py, notifications Telegram, HTTPS Tailscale."),
]

DEMARRAGE_TS = time.time()

PORT = 8585

# ── Dashboard servi à la racine ──────────────────────────────────
# cameras.html est le nom courant ; CameraOnOff.html est l'ancien nom,
# conservé en repli pour ne rien casser si le renommage n'est pas fait.
DASHBOARD_CANDIDATS = ["cameras.html", "CameraOnOff.html"]

def fichier_dashboard():
    """Retourne le chemin du dashboard réellement présent, ou None."""
    for nom in DASHBOARD_CANDIDATS:
        p = Path(__file__).parent / nom
        if p.exists():
            return p
    return None

# ── Certificat Tailscale : détecté AVANT la daemonisation pour que la
#    bannière annonce le bon schéma (une URL http:// alors que le serveur
#    écoute en TLS donne un ERR_CONNECTION_RESET très déroutant). ──
CERT_FILE = Path("/var/db/tailscale/imactavernier-2.tail78c299.ts.net.crt")
KEY_FILE  = Path("/var/db/tailscale/imactavernier-2.tail78c299.ts.net.key")
HTTPS_ACTIF = CERT_FILE.exists() and KEY_FILE.exists()
SCHEMA = "https" if HTTPS_ACTIF else "http"

NOMS_CAMERAS = [
    "Caméra - Terrasse",
    "Caméra - Salon",
    "Caméra - Bureau",
    "Caméra 2 eme génération",
]

URL_HOME   = "https://home.google.com"
MAX_ESSAIS = 5
ATTENTE    = 3

def _lire_credentials_telegram():
    """Lit token et chat_id depuis localisationParam.json (clé 'telegram').
    Retourne ("", "") si la clé est absente ou le fichier illisible."""
    try:
        param_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "localisationParam.json")
        with open(param_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tg = data.get("telegram", {})
        return str(tg.get("token", "")), str(tg.get("chat_id", ""))
    except Exception:
        return "", ""

_tg = _lire_credentials_telegram()
if not _tg[0] or not _tg[1]:
    print("⚠️  Telegram non configuré — ajouter la clé \"telegram\" dans localisationParam.json")

def envoyer_telegram(texte, markdown=True):
    """Envoie un message Telegram. Silencieux en cas d'échec.
    markdown=False pour le texte libre (zone de saisie dashboard, messages
    de changement d'état) : ces textes ne sont pas garantis "Markdown-safe"
    (guillemets, underscores, astérisques...) et un parse_mode="Markdown"
    invalide fait échouer l'envoi silencieusement côté API Telegram."""
    token, chat_id = _lire_credentials_telegram()   # relecture à chaque envoi (hot-reload)
    if not token or not chat_id:
        log("   📲 [Telegram] Credentials absents — envoi ignoré")
        return
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": texte}
    if markdown:
        payload["parse_mode"] = "Markdown"
    try:
        r = requests.post(url, data=payload, timeout=5)
        log(f"   📲 [Telegram] HTTP {r.status_code} — {r.text[:120]}")
    except Exception as e:
        log(f"   📲 [Telegram] Échec envoi : {e}")

# ═══════════════════════════════════════════════════════════════
# BUFFER DE LOGS (partagé serveur ↔ page HTML)
# ═══════════════════════════════════════════════════════════════

log_lock   = threading.Lock()
log_buffer = collections.deque(maxlen=200)  # 200 lignes max

debug_mode = False   # activé/désactivé via POST /debug depuis le dashboard

def log(msg):
    """Écrit dans le buffer ET dans stdout (redirigé vers /dev/null en background)."""
    ts  = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with log_lock:
        log_buffer.append(line)
    print(line, flush=True)

def dlog(msg):
    """Log de debug — ignoré si debug_mode est False."""
    if debug_mode:
        log(f"🔬 [DEBUG] {msg}")

# ═══════════════════════════════════════════════════════════════
# ÉTAT PARTAGÉ
# ═══════════════════════════════════════════════════════════════

camera_lock   = threading.Lock()
camera_status = {nom: None for nom in NOMS_CAMERAS}
camera_erreur = {nom: "ℹ️ Jamais interrogée depuis le démarrage du serveur" for nom in NOMS_CAMERAS}
camera_busy   = False

# Dernière action connue pour chaque caméra
# Structure : { nom: { "ts": "HH:MM:SS", "date": "JJ/MM/AAAA",
#                       "ancien": bool|None, "nouveau": bool|None,
#                       "source": str, "detail": str } }
camera_derniere_action = {nom: None for nom in NOMS_CAMERAS}

# Résultat du dernier test de diagnostic pour chaque caméra (bouton 🩺 Test).
# Rempli par tester_camera(), lu par le dashboard via /status (clé "diagnostics").
# Structure : voir _construire_diagnostic() plus bas.
camera_diagnostic = {nom: None for nom in NOMS_CAMERAS}

# Fichier d'historique persistant des changements d'état
HISTORIQUE_CAMERAS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historique_cameras.json")
# [AJOUT] Verrou dédié : _enregistrer_action() est appelée à la fois depuis
# basculer_camera() (thread toggle_worker) et depuis _check_state_changes_serveur()
# (thread lire_tous_les_etats, appelée APRÈS la libération de selenium_lock) —
# ces deux appels peuvent donc réellement se chevaucher dans le temps.
_historique_lock = threading.Lock()

def _ecrire_json_atomique(chemin, donnees):
    """Écrit `donnees` en JSON dans `chemin` de façon atomique (fichier
    temporaire puis os.replace), pour qu'un lecteur concurrent ne tombe
    jamais sur un fichier tronqué en cours d'écriture."""
    tmp = f"{chemin}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=2)
    os.replace(tmp, chemin)

# File d'attente des demandes de basculement — traitées une par une
toggle_queue = queue.Queue()

# Verrou global Selenium : une seule session Chrome à la fois
# (Chrome ne supporte pas deux instances simultanées du même profil)
selenium_lock = threading.Lock()

# ── [AJOUT MODE SERVEUR] ─────────────────────────────────────────────────────
# Tout ce bloc est inerte tant que proximite_mode_serveur est False.
# Aucune modification des chemins existants.

PORT_LOCALISATION = 8282          # port de localisation.py

proximite_mode_serveur  = False   # True = c'est le serveur qui gère la proximité
proximite_intervalle_s  = 120     # intervalle en secondes (envoyé par le HTML)
proximite_cameras       = []      # liste des noms de caméras cochées

_MAISON_DEFAUT = {"maison_lat": 48.83389, "maison_lon": 2.29546, "maison_rayon_km": 1.0}

def _charger_distances_locales():
    """Lit localisationParam.json directement (sans passer par le port 8282).
    Utilisé au démarrage et dans _tick_proximite pour disposer des valeurs
    même si localisation.py n'est pas encore lancé."""
    try:
        param_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "localisationParam.json")
        with open(param_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        d = data.get("distances", {})
        return {
            "maison_lat":        float(d.get("maison_lat",        _MAISON_DEFAUT["maison_lat"])),
            "maison_lon":        float(d.get("maison_lon",        _MAISON_DEFAUT["maison_lon"])),
            "maison_rayon_km":   float(d.get("maison_rayon_km",   _MAISON_DEFAUT["maison_rayon_km"])),
        }
    except Exception:
        return dict(_MAISON_DEFAUT)

_distances = _charger_distances_locales()
MAISON_LAT      = _distances["maison_lat"]
MAISON_LON      = _distances["maison_lon"]
MAISON_RAYON_KM = _distances["maison_rayon_km"]

_prox_timer_lock  = threading.Lock()
_prox_timer       = None           # threading.Timer en cours
_prox_timer_start = None           # time.time() au démarrage du timer
_actu_timer_start = None           # time.time() au démarrage du timer actu

def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat/2)**2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def _tick_proximite():
    """Exécuté par le timer périodique en mode SERVEUR.
    Interroge localisation.py, compare la position à laMaison,
    et déclenche les toggles nécessaires — exactement comme le faisait le HTML.
    """
    global proximite_mode_serveur
    if not proximite_mode_serveur:
        return
    # Replanifier le prochain tick AVANT l'opération (évite le glissement)
    _planifier_tick_proximite()

    # Relire les distances depuis le fichier (prend en compte les changements depuis le HTML)
    dist_params = _charger_distances_locales()
    maison_lat      = dist_params["maison_lat"]
    maison_lon      = dist_params["maison_lon"]
    maison_rayon_km = dist_params["maison_rayon_km"]

    log("🏠 [SERVEUR] Recherche de proximité laMaison…")
    try:
        # ── Récupérer la liste des personnes (URLs déclarées) et l'historique complet ──
        r_coord = requests.get(f"http://localhost:{PORT_LOCALISATION}/coordonnees", timeout=5)
        urls = r_coord.json().get("urls", [])
        if not urls:
            log("🏠 [SERVEUR] Aucune URL de coordonnées déclarée")
            return

        r_hist = requests.get(f"http://localhost:{PORT_LOCALISATION}/historique_positions.json", timeout=5)
        historique = r_hist.json()
        if not isinstance(historique, list):
            historique = []

        noms_fichiers = [u.rstrip("/").split("/")[-1] for u in urls]

        # ── Pour chaque personne, trouver sa DERNIÈRE position ──
        positions = []
        for nom in noms_fichiers:
            for entree in reversed(historique):
                if entree.get("nomfichier", "") != nom:
                    continue
                try:
                    lat = float(entree["latitude"])
                    lon = float(entree["longitude"])
                except (KeyError, TypeError, ValueError):
                    continue
                positions.append((nom, lat, lon))
                break

        if not positions:
            log("🏠 [SERVEUR] Aucune position GPS valide trouvée pour les personnes déclarées")
            return

        # ── Si AU MOINS UNE personne est proche de laMaison → caméras OFF ──
        une_proche = False
        for nom, lat, lon in positions:
            dist = _haversine_km(lat, lon, maison_lat, maison_lon)
            a_maison = dist < maison_rayon_km
            log(f"🏠 [SERVEUR] {nom} — distance maison : {dist*1000:.0f} m → {'À LA MAISON' if a_maison else 'ABSENT'}")
            if a_maison:
                une_proche = True

        cible = "off" if une_proche else "on"
        log(f"🏠 [SERVEUR] Décision : {'au moins une personne à la maison' if une_proche else 'toutes les personnes absentes'} → cible={cible.upper()}")

        with camera_lock:
            cams_a_traiter = [
                nom for nom in proximite_cameras
                if camera_status.get(nom) is not None          # état connu
                and camera_status[nom] != (cible == "on")      # pas déjà à la bonne valeur
            ]

        for nom in cams_a_traiter:
            log(f"🏠 [SERVEUR] Toggle automatique : {nom} → {cible.upper()}")
            _last_change_source[nom] = "proximite_gps"
            toggle_queue.put((nom, cible == "on"))

        if not cams_a_traiter:
            log("🏠 [SERVEUR] Toutes les caméras concernées sont déjà dans le bon état.")

    except Exception as e:
        log(f"🏠 [SERVEUR] Erreur interrogation localisation.py : {e}")

def _planifier_tick_proximite():
    """(Re)lance le timer périodique de proximité."""
    global _prox_timer, _prox_timer_start
    with _prox_timer_lock:
        if _prox_timer is not None:
            _prox_timer.cancel()
        if proximite_mode_serveur and proximite_intervalle_s > 0:
            _prox_timer = threading.Timer(proximite_intervalle_s, _tick_proximite)
            _prox_timer.daemon = True
            _prox_timer.start()
            _prox_timer_start = time.time()

def _arreter_timer_proximite():
    """Annule le timer en cours (basculement HTML→SERVEUR ou SERVEUR→HTML)."""
    global _prox_timer
    with _prox_timer_lock:
        if _prox_timer is not None:
            _prox_timer.cancel()
            _prox_timer = None

# ── Timer d'actualisation périodique côté serveur ───────────────────────────
actualisation_mode_serveur = False   # True = le serveur gère l'actualisation auto
actualisation_intervalle_s = 120     # intervalle en secondes

_actu_timer_lock = threading.Lock()
_actu_timer      = None

def _tick_actualisation():
    """Exécuté par le timer périodique d'actualisation en mode SERVEUR.
    Déclenche une relecture de l'état de toutes les caméras, exactement
    comme le ferait un clic 'Actualiser' depuis le HTML.
    """
    global actualisation_mode_serveur, camera_busy
    if not actualisation_mode_serveur:
        return
    # Replanifier AVANT l'opération pour éviter le glissement
    _planifier_tick_actualisation()

    log("🔄 [SERVEUR] Actualisation périodique automatique…")
    with camera_lock:
        if camera_busy:
            log("🔄 [SERVEUR] Serveur occupé, actualisation reportée au prochain tick")
            return
        # On marque busy pour bloquer les autres requêtes pendant la lecture
        camera_busy = True
    threading.Thread(target=lire_tous_les_etats, daemon=True).start()

def _planifier_tick_actualisation():
    """(Re)lance le timer périodique d'actualisation."""
    global _actu_timer, _actu_timer_start
    with _actu_timer_lock:
        if _actu_timer is not None:
            _actu_timer.cancel()
        if actualisation_mode_serveur and actualisation_intervalle_s > 0:
            _actu_timer = threading.Timer(actualisation_intervalle_s, _tick_actualisation)
            _actu_timer.daemon = True
            _actu_timer.start()
            _actu_timer_start = time.time()

def _arreter_timer_actualisation():
    """Annule le timer d'actualisation (basculement SERVEUR→HTML)."""
    global _actu_timer
    with _actu_timer_lock:
        if _actu_timer is not None:
            _actu_timer.cancel()
            _actu_timer = None

# ── [AJOUT] ── Persistance des modes serveur (localisationParam.json) ──────
# Les boutons "PROXIMITÉ : HTML/SERVEUR", "ACTUALISATION : HTML/SERVEUR" et
# "🔬 Debug" ne vivaient qu'en mémoire (perdus au redémarrage du process).
# On les sauvegarde dans localisationParam.json via localisation.py
# (clé "dashboard"), et on les restaure au démarrage.

def _charger_preferences_serveur():
    """Récupère le dict 'preferences' depuis localisation.py (port 8282)."""
    try:
        r = requests.get(f"http://localhost:{PORT_LOCALISATION}/preferences", timeout=5)
        d = r.json()
        if d.get("ok") and isinstance(d.get("preferences"), dict):
            return d["preferences"]
    except Exception as e:
        log(f"⚠️ Impossible de charger les préférences serveur : {e}")
    return {}


def _sauvegarder_preferences_serveur(valeurs):
    """Fusionne et sauvegarde des valeurs dans localisationParam.json (clé 'dashboard')."""
    try:
        requests.post(
            f"http://localhost:{PORT_LOCALISATION}/preferences",
            json=valeurs, timeout=5,
        )
    except Exception as e:
        log(f"⚠️ Impossible de sauvegarder les préférences serveur : {e}")


def _restaurer_modes_serveur():
    """Restaure proximité / actualisation / debug depuis localisationParam.json
    au démarrage de cameraServer.py, et relance les timers si besoin."""
    global proximite_mode_serveur, proximite_intervalle_s, proximite_cameras
    global actualisation_mode_serveur, actualisation_intervalle_s, debug_mode

    prefs = _charger_preferences_serveur()
    if not prefs:
        return

    prox = prefs.get("proximite")
    if isinstance(prox, dict):
        proximite_mode_serveur = bool(prox.get("mode_serveur", proximite_mode_serveur))
        proximite_intervalle_s = int(prox.get("intervalle_s", proximite_intervalle_s))
        cameras = prox.get("cameras")
        if isinstance(cameras, list):
            proximite_cameras = [n for n in cameras if n in NOMS_CAMERAS]
        if proximite_mode_serveur:
            _planifier_tick_proximite()
            log(f"🏠 [SERVEUR] Mode proximité restauré → SERVEUR — intervalle={proximite_intervalle_s}s — caméras={proximite_cameras}")

    actu = prefs.get("actualisation")
    if isinstance(actu, dict):
        actualisation_mode_serveur = bool(actu.get("mode_serveur", actualisation_mode_serveur))
        actualisation_intervalle_s = int(actu.get("intervalle_s", actualisation_intervalle_s))
        if actualisation_mode_serveur:
            _planifier_tick_actualisation()
            log(f"🔄 [SERVEUR] Mode actualisation restauré → SERVEUR — intervalle={actualisation_intervalle_s}s")

    if "debug" in prefs:
        debug_mode = bool(prefs.get("debug", debug_mode))
        if debug_mode:
            log("🔬 Mode debug restauré → activé")
# ── [FIN AJOUT] ──────────────────────────────────────────────────────────────

# ── Détection de changements d'état lors d'un scan (équivalent checkStateChanges JS) ──
# Inerte tant que actualisation_mode_serveur est False.

_previous_state      = {nom: None for nom in NOMS_CAMERAS}
_previous_state_lock = threading.Lock()
_last_change_source  = {}   # nom -> "manual" | "auto_maison" | None

def _enregistrer_action(nom, ancien, nouveau, source, detail=''):
    """Mémorise le dernier changement d'état d'une caméra en mémoire et sur disque."""
    entree = {
        'ts':      time.strftime('%H:%M:%S'),
        'date':    time.strftime('%d/%m/%Y'),
        'datetime': time.strftime('%Y-%m-%d %H:%M:%S'),
        'camera':  nom,
        'ancien':  'ON' if ancien else 'OFF',
        'nouveau': 'ON' if nouveau else 'OFF',
        'source':  source,
        'detail':  detail,
    }
    with camera_lock:
        camera_derniere_action[nom] = {
            'ts':      entree['ts'],
            'date':    entree['date'],
            'ancien':  ancien,
            'nouveau': nouveau,
            'source':  source,
            'detail':  detail,
        }
    # Écriture persistante sur disque
    try:
        with _historique_lock:
            historique = []
            if os.path.exists(HISTORIQUE_CAMERAS_FILE):
                with open(HISTORIQUE_CAMERAS_FILE, 'r', encoding='utf-8') as f:
                    historique = json.load(f)
            historique.append(entree)
            # Garder les 1000 dernières entrées
            historique = historique[-1000:]
            _ecrire_json_atomique(HISTORIQUE_CAMERAS_FILE, historique)
    except Exception as e:
        log(f"⚠️ Erreur écriture historique caméras : {e}")

def _check_state_changes_serveur(nouveaux_etats):
    changements = []
    with _previous_state_lock:
        for nom in NOMS_CAMERAS:
            ancien  = _previous_state.get(nom)
            nouveau = nouveaux_etats.get(nom)
            if (ancien in (True, False)
                    and nouveau in (True, False)
                    and ancien != nouveau):
                action = "activee" if nouveau else "desactivee"
                emoji  = "\U0001f7e2" if nouveau else "\U0001f534"
                src    = _last_change_source.get(nom)
                if src == "manual":
                    prefixe = "🖱️ Action manuelle depuis le dashboard"
                elif src == "proximite_gps":
                    prefixe = "🏠 Automatisation proximité GPS"
                elif src == "auto_maison":
                    prefixe = "🏠 Automatisation laMaison (proximité GPS)"
                else:
                    prefixe = "📱 Changement détecté via Google Home ou app externe"
                label = nom
                changements.append(
                    prefixe + "\n" +
                    "Camera \"" + label + "\" " + ("activee " if nouveau else "desactivee ") + emoji
                )
                log("   Changement detecte [" + nom + "] : "
                    + ("ON" if ancien else "OFF") + " -> "
                    + ("ON" if nouveau else "OFF")
                    + " (" + prefixe + ")")
                if src in ("manual", "proximite_gps", "auto_maison"):
                    detail_scan = "Changement confirmé lors du scan périodique."
                else:
                    detail_scan = "Changement détecté lors d'un scan d'état périodique — probablement effectué via l'application Google Home ou une autre app externe."
                _enregistrer_action(nom, ancien, nouveau, prefixe, detail=detail_scan)
            if nouveau in (True, False):
                _previous_state[nom] = nouveau
    if changements:
        envoyer_telegram("\n\n".join(changements))

# ── [FIN AJOUT MODE SERVEUR] ─────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════
# SELENIUM
# ═══════════════════════════════════════════════════════════════

def creer_driver():
    opts = Options()
    opts.binary_location = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    profil_dir = Path(__file__).parent / "chrome_profile"
    opts.add_argument(f"--user-data-dir={profil_dir}")
    driver = webdriver.Chrome(options=opts)
    return driver

ATTENTE_PAGE_MAX = 20   # secondes max pour que les widgets caméra apparaissent

def charger_page(driver):
    driver.get(URL_HOME)
    for tick in range(ATTENTE_PAGE_MAX):
        time.sleep(1)
        if "accounts.google.com" in driver.current_url:
            return False
        # Chercher "videocam" uniquement dans les cartes caméra connues
        cartes_pretes = 0
        for nom in NOMS_CAMERAS:
            try:
                el = driver.find_element(By.XPATH,
                    f"//*[normalize-space(text())='{nom}' or contains(text(),'{nom[:15]}')]")
                carte = el.find_element(By.XPATH, "ancestor::*[5]")
                # La carte est "prête" si elle contient un widget videocam
                if carte.find_elements(By.XPATH, ".//*[contains(text(),'videocam')]"):
                    cartes_pretes += 1
            except Exception:
                pass
        if cartes_pretes >= len(NOMS_CAMERAS):
            log(f"   Page prête en {tick + 1}s ({cartes_pretes} cartes chargées)")
            return True
    log(f"   ⚠️ Timeout {ATTENTE_PAGE_MAX}s — on tente quand même")
    return True

def lire_etat(driver, nom):
    try:
        el_nom = driver.find_element(
            By.XPATH,
            f"//*[normalize-space(text())='{nom}' or contains(text(),'{nom[:15]}')]"
        )
        try:
            carte = el_nom.find_element(By.XPATH, "ancestor::*[5]")
        except Exception:
            carte = el_nom

        lignes = [l.strip() for l in carte.text.lower().splitlines() if l.strip()]
        dlog(f"lire_etat [{nom}] lignes={lignes}")

        # "en direct" peut être fusionné avec un nom d'icône (ex: "videocamen direct")
        # → sous-chaîne sur ligne courte uniquement
        if any("en direct" in l and len(l) < 40 for l in lignes):
            return True
        # "caméra désactivée" est spécifique et fiable
        if any(l == "caméra désactivée" for l in lignes):
            return False

        # Fallback : inspecter les boutons dans la carte
        for btn in carte.find_elements(By.TAG_NAME, "button"):
            txt = (btn.text or btn.get_attribute("aria-label") or "").lower()
            dlog(f"  lire_etat <button> text={txt!r}")
            if "activer" in txt and "désactiver" not in txt:
                return False
            if "désactiver" in txt or "éteindre" in txt:
                return True
    except Exception as e:
        dlog(f"Exception lire_etat [{nom}] : {e}")
    return None

def trouver_bouton_activer(driver, nom):
    try:
        el_nom = driver.find_element(
            By.XPATH,
            f"//*[normalize-space(text())='{nom}' or contains(text(),'{nom[:15]}')]"
        )
        carte = el_nom.find_element(By.XPATH, "ancestor::*[5]")

        dlog(f"Carte [{nom}] texte brut : {carte.text[:200]!r}")
        for btn in carte.find_elements(By.TAG_NAME, "button"):
            txt  = btn.text or ""
            aria = btn.get_attribute("aria-label") or ""
            dlog(f"  <button> text={txt!r}  aria-label={aria!r}")
        for el in carte.find_elements(By.CSS_SELECTOR, "[role='button'],[role='switch']"):
            txt  = el.text or ""
            aria = el.get_attribute("aria-label") or ""
            role = el.get_attribute("role") or ""
            dlog(f"  role={role!r} text={txt!r}  aria-label={aria!r}")

        for btn in carte.find_elements(By.TAG_NAME, "button"):
            txt = (btn.text or btn.get_attribute("aria-label") or "").lower()
            if "activer" in txt and "désactiver" not in txt:
                return btn
    except Exception as e:
        dlog(f"Exception trouver_bouton_activer : {e}")
    return None

def trouver_bouton_desactiver(driver, nom):
    try:
        el_nom = driver.find_element(
            By.XPATH,
            f"//*[normalize-space(text())='{nom}' or contains(text(),'{nom[:15]}')]"
        )
        carte = el_nom.find_element(By.XPATH, "ancestor::*[5]")

        dlog(f"Carte [{nom}] texte brut : {carte.text[:200]!r}")
        for btn in carte.find_elements(By.TAG_NAME, "button"):
            txt  = btn.text or ""
            aria = btn.get_attribute("aria-label") or ""
            dlog(f"  <button> text={txt!r}  aria-label={aria!r}")
        for el in carte.find_elements(By.CSS_SELECTOR, "[role='button'],[role='switch']"):
            txt  = el.text or ""
            aria = el.get_attribute("aria-label") or ""
            role = el.get_attribute("role") or ""
            dlog(f"  role={role!r} text={txt!r}  aria-label={aria!r}")

        for btn in carte.find_elements(By.TAG_NAME, "button"):
            txt = (btn.text or btn.get_attribute("aria-label") or "").lower()
            if any(w in txt for w in ["désactiver", "éteindre", "turn off"]):
                return btn, False
    except Exception as e:
        dlog(f"Exception trouver_bouton_desactiver (phase 1) : {e}")

    # Ouvrir le panel détail
    try:
        el_nom = driver.find_element(
            By.XPATH,
            f"//*[normalize-space(text())='{nom}' or contains(text(),'{nom[:15]}')]"
        )
        el_nom.click()
        time.sleep(2)
        dlog(f"Panel ouvert pour [{nom}], scan des boutons...")
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            txt  = btn.text or ""
            aria = btn.get_attribute("aria-label") or ""
            dlog(f"  panel <button> text={txt!r}  aria-label={aria!r}")
            if any(w in (txt + aria).lower() for w in ["désactiver", "éteindre", "turn off", "off"]):
                return btn, True
        for sw in driver.find_elements(By.CSS_SELECTOR, "[role='switch']"):
            checked = sw.get_attribute("aria-checked")
            aria    = sw.get_attribute("aria-label") or ""
            dlog(f"  panel [role=switch] aria-checked={checked!r}  aria-label={aria!r}")
            if checked == "true":
                return sw, True
    except Exception as e:
        dlog(f"Exception trouver_bouton_desactiver (panel) : {e}")
    return None, False

def fermer_panel(driver):
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        time.sleep(1)
    except Exception:
        pass

def lire_tous_les_etats():
    global camera_busy
    log("📖 Lecture état de toutes les caméras...")
    horodatage = time.strftime("%H:%M:%S")
    driver = None
    # snapshot avant lecture pour détection de changements
    with camera_lock:
        etats_avant = dict(camera_status)
    nouveaux_etats = {}

    # [FIX] Verrou Selenium : empêche qu'une lecture et un basculement
    # lancent deux instances Chrome sur le même profil simultanément.
    if not selenium_lock.acquire(blocking=False):
        log("⚠️ Lecture annulée : une session Selenium est déjà en cours")
        with camera_lock:
            camera_busy = False
        return

    try:
        try:
            driver = creer_driver()
        except Exception as e:
            msg = f"⚠️ [{horodatage}] Échec démarrage du navigateur Chrome/Selenium : {e}"
            log(f"❌ Erreur lecture : {e}")
            with camera_lock:
                for nom in NOMS_CAMERAS:
                    camera_erreur[nom] = msg
            return

        if not charger_page(driver):
            msg = (f"⚠️ [{horodatage}] La page Google Home n'a pas pu être chargée "
                   f"(session Google probablement expirée — relancer le script avec --setup)")
            log("❌ Session Google expirée — relancer --setup")
            with camera_lock:
                for nom in NOMS_CAMERAS:
                    camera_erreur[nom] = msg
            return

        for nom in NOMS_CAMERAS:
            try:
                etat = lire_etat(driver, nom)
            except Exception as e:
                etat = None
                with camera_lock:
                    camera_erreur[nom] = (
                        f"⚠️ [{horodatage}] Erreur lors de la lecture de la carte "
                        f"« {nom} » sur Google Home : {e}"
                    )
                nouveaux_etats[nom] = etat
                log(f"   {nom} → ERREUR : {e}")
                continue

            with camera_lock:
                camera_status[nom] = etat
                if etat is None:
                    camera_erreur[nom] = (
                        f"⚠️ [{horodatage}] Carte « {nom} » introuvable ou état non "
                        f"reconnu sur la page Google Home (mise en page modifiée ?)"
                    )
                else:
                    camera_erreur[nom] = None
            nouveaux_etats[nom] = etat
            etat_str = "ON" if etat is True else ("OFF" if etat is False else "?")
            log(f"   {nom} → {etat_str}")
    except Exception as e:
        msg = f"⚠️ [{horodatage}] Erreur inattendue pendant la lecture : {e}"
        log(f"❌ Erreur lecture : {e}")
        with camera_lock:
            for nom in NOMS_CAMERAS:
                if camera_status.get(nom) is None and camera_erreur.get(nom) is None:
                    camera_erreur[nom] = msg
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        selenium_lock.release()  # [FIX] libérer le verrou dans tous les cas
        with camera_lock:
            camera_busy = False
        log("✅ Lecture terminée")
    # détecter les changements si mode serveur actif
    if actualisation_mode_serveur and nouveaux_etats:
        _check_state_changes_serveur(nouveaux_etats)

# ═══════════════════════════════════════════════════════════════
# TEST / DIAGNOSTIC D'UNE CAMÉRA (bouton 🩺 Test du dashboard)
# ═══════════════════════════════════════════════════════════════
#
# Objectif : dire, pour UNE caméra, si elle répond normalement, et sinon
# poser un diagnostic (hors ligne, session Google expirée, carte introuvable,
# état illisible…) + proposer des actions correctives concrètes.
#
# Le test réutilise exactement la même mécanique Selenium que la lecture
# d'état (creer_driver / charger_page / lire_etat) et respecte le même
# verrou (selenium_lock) + le drapeau camera_busy, pour ne jamais lancer
# deux sessions Chrome simultanées sur le même profil.

# Chaînes affichées par Google Home / Nest quand une caméra est injoignable.
# Volontairement large : si un cas n'est pas reconnu, le "texte brut" renvoyé
# au dashboard permet de repérer la formulation exacte et de compléter la liste.
_MARQUEURS_HORS_LIGNE = [
    "hors ligne", "hors-ligne", "hors connexion", "hors-connexion", "hors tension",
    "impossible de se connecter", "impossible de contacter", "impossible d'accéder",
    "vérifiez votre connexion", "vérifie ta connexion", "problème de connexion",
    "aucun aperçu", "aucun signal", "pas de signal", "aucune connexion", "aucun flux",
    "appareil indisponible", "caméra indisponible", "flux indisponible", "indisponible",
    "connexion perdue", "perte de connexion", "reconnexion", "reconnexion en cours",
    "déconnecté", "déconnectée", "non connecté", "non connectée", "ne répond pas",
    "offline", "no signal", "can't connect", "cannot connect", "check your connection",
    "reconnecting", "unavailable", "disconnected", "not responding", "no preview",
]


def _construire_diagnostic(nom, statut, icone, titre, message, cause, actions,
                           texte_brut="", boutons=None):
    """Fabrique le dict de diagnostic renvoyé au dashboard.

    statut ∈ {ok_on, ok_off, offline, session_expired, card_not_found,
              unknown_state, erreur}
    actions : liste de dicts. Types compris par le dashboard :
        {"type": "toggle",   "label": ..., "target": "on"|"off"}
        {"type": "retest",   "label": ...}
        {"type": "open_url",  "label": ..., "url": ...}
        {"type": "command",   "label": ..., "command": ...}
        {"type": "instruction","label": ...}
    """
    return {
        "camera":    nom,
        "ts":        time.strftime("%H:%M:%S"),
        "date":      time.strftime("%d/%m/%Y"),
        "datetime":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "statut":    statut,
        "icone":     icone,
        "titre":     titre,
        "message":   message,
        "cause":     cause,
        "actions":   actions or [],
        "texte_brut": texte_brut,
        "boutons":   boutons or [],
    }


def _stocker_diagnostic(nom, diag):
    with camera_lock:
        camera_diagnostic[nom] = diag
    log(f"🩺 [{nom}] Diagnostic : {diag['statut']} — {diag['titre']}")


def _classer_diagnostic(nom, texte, boutons):
    """Analyse le texte + les boutons d'une carte caméra trouvée sur Google Home
    et en déduit un diagnostic structuré."""
    texte_low = texte.lower()
    lignes = [l.strip() for l in texte_low.splitlines() if l.strip()]

    live       = any("en direct" in l and len(l) < 40 for l in lignes)
    desactivee = any(l == "caméra désactivée" for l in lignes)
    hors_ligne = any(m in texte_low for m in _MARQUEURS_HORS_LIGNE)

    # 1) Flux en direct visible → la caméra fonctionne et est allumée
    if live:
        return _construire_diagnostic(
            nom, "ok_on", "🟢",
            "Caméra opérationnelle (allumée)",
            "La caméra répond normalement et diffuse en direct. Aucune action nécessaire.",
            None,
            [
                {"type": "toggle", "label": "Forcer la désactivation (OFF)", "target": "off"},
                {"type": "retest", "label": "Relancer le test"},
            ],
            texte_brut=texte, boutons=boutons,
        )

    # 2) Caméra explicitement désactivée → elle fonctionne, mais éteinte
    if desactivee and not hors_ligne:
        return _construire_diagnostic(
            nom, "ok_off", "⚪",
            "Caméra opérationnelle (désactivée)",
            "La caméra répond mais elle est actuellement désactivée. "
            "C'est un état normal — si tu attendais qu'elle soit allumée, active-la ci-dessous.",
            None,
            [
                {"type": "toggle", "label": "Activer la caméra (ON)", "target": "on"},
                {"type": "retest", "label": "Relancer le test"},
            ],
            texte_brut=texte, boutons=boutons,
        )

    # 3) Marqueur "hors ligne / indisponible" détecté → caméra injoignable
    if hors_ligne:
        return _construire_diagnostic(
            nom, "offline", "🔴",
            "Caméra hors ligne / injoignable",
            "Google Home indique que cette caméra est actuellement injoignable. "
            "Le problème vient de la caméra elle-même (alimentation ou réseau), "
            "pas du dashboard : les autres caméras, elles, restent pilotables.",
            "Cause probable : coupure d'alimentation, perte du Wi-Fi, ou caméra à redémarrer.",
            [
                {"type": "instruction",
                 "label": "1. Vérifier l'alimentation : débrancher la caméra 10 s puis la rebrancher, "
                          "et attendre ~1 à 2 min qu'elle redémarre."},
                {"type": "instruction",
                 "label": "2. Vérifier le Wi-Fi de la maison (box allumée, réseau accessible) et "
                          "que la caméra est à portée du point d'accès."},
                {"type": "instruction",
                 "label": "3. Ouvrir l'app Google Home sur le téléphone et noter le message d'état "
                          "exact de la caméra (utile si le problème persiste)."},
                {"type": "open_url", "label": "Ouvrir Google Home dans le navigateur",
                 "url": "https://home.google.com"},
                {"type": "retest", "label": "Relancer le test après manipulation"},
            ],
            texte_brut=texte, boutons=boutons,
        )

    # 4) Repli sur les boutons de la carte (activer / désactiver)
    etat_btn = None
    for b in boutons:
        bl = b.lower()
        if "activer" in bl and "désactiver" not in bl:
            etat_btn = False
        elif "désactiver" in bl or "éteindre" in bl:
            etat_btn = True
    if etat_btn is True:
        return _construire_diagnostic(
            nom, "ok_on", "🟢",
            "Caméra opérationnelle (allumée)",
            "La caméra répond (contrôle de désactivation présent). Aucune action nécessaire.",
            None,
            [
                {"type": "toggle", "label": "Forcer la désactivation (OFF)", "target": "off"},
                {"type": "retest", "label": "Relancer le test"},
            ],
            texte_brut=texte, boutons=boutons,
        )
    if etat_btn is False:
        return _construire_diagnostic(
            nom, "ok_off", "⚪",
            "Caméra opérationnelle (désactivée)",
            "La caméra répond (contrôle d'activation présent) mais elle est éteinte.",
            None,
            [
                {"type": "toggle", "label": "Activer la caméra (ON)", "target": "on"},
                {"type": "retest", "label": "Relancer le test"},
            ],
            texte_brut=texte, boutons=boutons,
        )

    # 5) Carte trouvée mais état illisible
    return _construire_diagnostic(
        nom, "unknown_state", "🟠",
        "État indéterminé",
        "La carte de la caméra a bien été trouvée sur Google Home, mais son état n'a "
        "pas pu être interprété. Cela arrive quand Google modifie la mise en page, ou "
        "quand la carte est en cours de chargement.",
        "Cause probable : mise en page Google Home modifiée, ou caméra en cours de connexion.",
        [
            {"type": "retest", "label": "Relancer le test"},
            {"type": "open_url", "label": "Ouvrir Google Home dans le navigateur",
             "url": "https://home.google.com"},
            {"type": "instruction",
             "label": "Consulter le « texte brut » ci-dessous : il montre ce que Google Home "
                      "affiche réellement pour cette carte (utile pour affiner la détection)."},
        ],
        texte_brut=texte, boutons=boutons,
    )


def tester_camera(nom):
    """Teste UNE caméra et stocke le diagnostic dans camera_diagnostic[nom].
    Exécuté dans un thread dédié (lancé par la route POST /test-camera)."""
    global camera_busy
    log(f"🩺 Test de la caméra [{nom}]…")
    driver = None

    # Verrou Selenium non bloquant : si une autre session tourne déjà, on
    # renvoie un diagnostic explicite plutôt que d'attendre indéfiniment.
    if not selenium_lock.acquire(blocking=False):
        log("⚠️ Test annulé : une session Selenium est déjà en cours")
        _stocker_diagnostic(nom, _construire_diagnostic(
            nom, "erreur", "⏳",
            "Serveur occupé",
            "Une autre opération (lecture, basculement ou test) est en cours. "
            "Réessaie dans quelques secondes.",
            None,
            [{"type": "retest", "label": "Relancer le test"}],
        ))
        with camera_lock:
            camera_busy = False
        return

    try:
        # ── Démarrage du navigateur ──
        try:
            driver = creer_driver()
        except Exception as e:
            _stocker_diagnostic(nom, _construire_diagnostic(
                nom, "erreur", "❌",
                "Navigateur indisponible",
                f"Chrome / Selenium n'a pas pu démarrer : {e}",
                "Cause probable : Chrome absent, mis à jour, ou profil verrouillé par une session restée ouverte.",
                [
                    {"type": "command", "label": "Redémarrer le serveur caméras (Terminal du Mac)",
                     "command": 'pkill -f cameraServer.py ; sleep 1 ; python3.13 cameraServer.py'},
                    {"type": "retest", "label": "Relancer le test"},
                ],
            ))
            return

        # ── Chargement de la page Google Home ──
        try:
            charger_page(driver)
        except Exception as e:
            dlog(f"tester_camera : exception charger_page : {e}")

        # Session Google expirée → redirection vers la page de connexion
        if "accounts.google.com" in (driver.current_url or ""):
            _stocker_diagnostic(nom, _construire_diagnostic(
                nom, "session_expired", "🔑",
                "Session Google expirée",
                "Le dashboard n'est plus connecté au compte Google : impossible de lire "
                "l'état des caméras tant que la session n'est pas rouverte. Ce problème "
                "concerne TOUTES les caméras, pas seulement celle-ci.",
                "Cause probable : déconnexion Google après plusieurs jours / semaines.",
                [
                    {"type": "command",
                     "label": "1. Rouvrir la session Google (Terminal du Mac)",
                     "command": "python3.13 cameraControl.py --setup"},
                    {"type": "command",
                     "label": "2. Relancer le serveur caméras",
                     "command": "pkill -f cameraServer.py ; sleep 1 ; python3.13 cameraServer.py"},
                    {"type": "retest", "label": "Relancer le test"},
                ],
            ))
            return

        # ── Recherche de la carte de la caméra ──
        try:
            el_nom = driver.find_element(
                By.XPATH,
                f"//*[normalize-space(text())='{nom}' or contains(text(),'{nom[:15]}')]"
            )
        except Exception:
            _stocker_diagnostic(nom, _construire_diagnostic(
                nom, "card_not_found", "❓",
                "Caméra introuvable sur Google Home",
                f"Aucune carte nommée « {nom} » n'a été trouvée sur la page Google Home. "
                "La caméra a peut-être été renommée, retirée du domicile, ou la page n'a "
                "pas fini de charger.",
                "Cause probable : caméra renommée / supprimée, ou nom différent de celui déclaré.",
                [
                    {"type": "open_url", "label": "Ouvrir Google Home pour vérifier le nom",
                     "url": "https://home.google.com"},
                    {"type": "instruction",
                     "label": f"Vérifier que la caméra s'appelle toujours exactement « {nom} ». "
                              "Si son nom a changé, mettre à jour NOMS_CAMERAS dans cameraServer.py "
                              "et CAMERAS dans cameras.html."},
                    {"type": "retest", "label": "Relancer le test"},
                ],
            ))
            # Marquer l'état inconnu côté carte principale
            with camera_lock:
                camera_status[nom] = None
                camera_erreur[nom] = (
                    f"⚠️ [{time.strftime('%H:%M:%S')}] Carte « {nom} » introuvable "
                    f"sur Google Home (renommée / retirée ?)"
                )
            return

        # ── Extraction du texte et des boutons de la carte ──
        try:
            carte = el_nom.find_element(By.XPATH, "ancestor::*[5]")
        except Exception:
            carte = el_nom
        texte = carte.text or ""
        boutons = []
        try:
            for btn in carte.find_elements(By.TAG_NAME, "button"):
                t = (btn.text or btn.get_attribute("aria-label") or "").strip()
                if t:
                    boutons.append(t)
        except Exception:
            pass

        diag = _classer_diagnostic(nom, texte, boutons)
        _stocker_diagnostic(nom, diag)

        # Synchroniser la carte principale avec ce que le test vient d'établir
        with camera_lock:
            if diag["statut"] == "ok_on":
                camera_status[nom] = True
                camera_erreur[nom] = None
            elif diag["statut"] == "ok_off":
                camera_status[nom] = False
                camera_erreur[nom] = None
            else:
                camera_status[nom] = None
                camera_erreur[nom] = f"⚠️ {diag['titre']} — {diag['message']}"

    except Exception as e:
        log(f"❌ Erreur pendant le test [{nom}] : {e}")
        _stocker_diagnostic(nom, _construire_diagnostic(
            nom, "erreur", "❌",
            "Erreur pendant le test",
            f"Une erreur inattendue est survenue : {e}",
            None,
            [{"type": "retest", "label": "Relancer le test"}],
        ))
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        selenium_lock.release()
        with camera_lock:
            camera_busy = False
        log(f"🩺 Test [{nom}] terminé")


def basculer_camera(nom, cible_on):
    global camera_busy
    cible_str = "ON" if cible_on else "OFF"
    log(f"🔄 Basculement [{nom}] → {cible_str}")
    horodatage = time.strftime("%H:%M:%S")
    driver = None

    # [FIX] Verrou Selenium : garantit qu'aucune autre session Chrome ne tourne
    # sur le même profil pendant ce basculement.
    selenium_lock.acquire()

    try:
        try:
            driver = creer_driver()
        except Exception as e:
            log(f"❌ Erreur basculement : {e}")
            with camera_lock:
                camera_erreur[nom] = (
                    f"⚠️ [{horodatage}] Échec démarrage du navigateur Chrome/Selenium "
                    f"pendant le basculement de « {nom} » → {cible_str} : {e}"
                )
            return
        if not charger_page(driver):
            log("❌ Session expirée")
            with camera_lock:
                camera_erreur[nom] = (
                    f"⚠️ [{horodatage}] La page Google Home n'a pas pu être chargée "
                    f"pendant le basculement de « {nom} » → {cible_str} "
                    f"(session Google probablement expirée — relancer le script avec --setup)"
                )
            return
        succes = False
        for essai in range(1, MAX_ESSAIS + 1):
            log(f"   Essai {essai}/{MAX_ESSAIS}...")
            etat = lire_etat(driver, nom)
            etat_str = "ON" if etat is True else ("OFF" if etat is False else "?")
            log(f"   État lu : {etat_str}")
            with camera_lock:
                camera_status[nom] = etat
            if etat == cible_on:
                log(f"   ✅ Objectif atteint !")
                emoji  = "🟢" if cible_on else "🔴"
                action = "activée" if cible_on else "désactivée"
                envoyer_telegram(f"{emoji} *{nom}* {action}")
                # Mémoriser la source et le contexte du changement
                src = _last_change_source.get(nom)
                if src == "proximite_gps":
                    src_label = "🏠 Automatisation proximité GPS"
                    detail = "Changement déclenché automatiquement par le système de localisation GPS : présence ou absence d'un occupant détectée."
                elif src == "auto_maison":
                    src_label = "🏠 Automatisation laMaison (proximité GPS)"
                    detail = "Changement déclenché automatiquement par la règle laMaison suite à la détection GPS d'un occupant."
                elif src == "manual":
                    src_label = "🖱️ Action manuelle depuis le dashboard"
                    detail = "Changement effectué manuellement depuis l'interface web."
                else:
                    src_label = "❓ Source inconnue"
                    detail = "La source de ce changement n'a pas pu être déterminée."
                ancien_etat = not cible_on
                _enregistrer_action(nom, ancien_etat, cible_on, src_label, detail=detail)
                succes = True
                break
            if cible_on:
                bouton = trouver_bouton_activer(driver, nom)
                if bouton:
                    log(f"   🖱️  Clic 'Activer'")
                    driver.execute_script("arguments[0].scrollIntoView(true);", bouton)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", bouton)
                else:
                    log(f"   ⚠️  Bouton 'Activer' introuvable")
            else:
                bouton, panel = trouver_bouton_desactiver(driver, nom)
                if bouton:
                    log(f"   🖱️  Clic 'Désactiver'")
                    driver.execute_script("arguments[0].scrollIntoView(true);", bouton)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", bouton)
                    if panel:
                        time.sleep(1)
                        fermer_panel(driver)
                else:
                    log(f"   ⚠️  Bouton 'Désactiver' introuvable")
            log(f"   ⏳ Attente {ATTENTE}s...")
            time.sleep(ATTENTE)
            charger_page(driver)

        # [FIX] Lecture finale protégée : la fenêtre peut être fermée si Chrome
        # a planté ou redirigé — on attrape l'exception proprement.
        try:
            etat_final = lire_etat(driver, nom)
        except Exception as e:
            log(f"   ⚠️  Lecture finale impossible (fenêtre fermée ?) : {e}")
            etat_final = None

        # Si le basculement a été confirmé pendant la boucle mais que la lecture
        # finale retourne None (timing : page pas encore stabilisée), on conserve
        # l'état connu plutôt que d'émettre un faux avertissement.
        if etat_final is None and succes:
            etat_final = cible_on
            log(f"   ⚠️  Lecture finale indisponible — état conservé depuis confirmation boucle")

        with camera_lock:
            camera_status[nom] = etat_final
            if etat_final is None:
                camera_erreur[nom] = (
                    f"⚠️ [{horodatage}] Carte « {nom} » introuvable ou état non "
                    f"reconnu sur la page Google Home après le basculement → {cible_str} "
                    f"(mise en page modifiée ?)"
                )
            else:
                camera_erreur[nom] = None
        log(f"   État final : {'ON' if etat_final else 'OFF'}")
    except Exception as e:
        log(f"❌ Erreur basculement : {e}")
        with camera_lock:
            camera_erreur[nom] = (
                f"⚠️ [{horodatage}] Erreur inattendue pendant le basculement de "
                f"« {nom} » → {cible_str} : {e}"
            )
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        selenium_lock.release()  # [FIX] libérer le verrou dans tous les cas
        with camera_lock:
            camera_busy = False
        log("✅ Opération terminée")

def toggle_worker():
    """Thread unique qui consomme la file d'attente des basculements."""
    global camera_busy
    while True:
        try:
            # Attend de manière bloquante qu'une action arrive
            nom, cible_on = toggle_queue.get()
            
            with camera_lock:
                camera_busy = True
            
            try:
                basculer_camera(nom, cible_on)
            except Exception as e:
                log(f"❌ Erreur critique dans le worker de basculement : {e}")
                time.sleep(2) # Évite l'emballage CPU si Selenium plante en boucle
            finally:
                toggle_queue.task_done()
                
        except Exception as e:
            log(f"❌ Erreur boucle principale worker : {e}")
            time.sleep(1)

# ═══════════════════════════════════════════════════════════════
# SERVEUR HTTP
# ═══════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silencer les logs HTTP natifs (on gère nous-mêmes)

    def log_request(self, code='-', size='-'):
        """Log minimal à chaque requête : méthode, URL, code HTTP, durée."""
        duree_ms = int((time.time() - self._t0) * 1000) if hasattr(self, '_t0') else -1
        client = self.address_string()
        # Ne pas logger les requêtes de polling fréquentes pour ne pas polluer
        routes_silencieuses = ('/logs', '/status', '/loc/', '/refresh', '/proximite-status', '/actualisation-status', '/debug-status', '/clear-logs', '/server-countdown')
        if not any(self.path.startswith(r) for r in routes_silencieuses):
            log(f"→ {self.command} {self.path} [{code}] {duree_ms}ms — {client}")

    # ── En-têtes communs à TOUTES les réponses ──────────────────
    # CORS : indispensable si la page est ouverte en file:// (origine "null")
    #        ou depuis GitHub Pages.
    # no-store : sans cela, le navigateur ressert un cameras.html périmé
    #            après un redéploiement, ce qui fait perdre un temps fou.
    def _entetes_communs(self, mime, taille):
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(taille))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Camera-Server-Version", VERSION_SERVEUR)

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self._entetes_communs("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    MIMES = {
        ".html": "text/html; charset=utf-8",
        ".css":  "text/css; charset=utf-8",
        ".js":   "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".txt":  "text/plain; charset=utf-8",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif":  "image/gif",
        ".svg":  "image/svg+xml",
        ".ico":  "image/x-icon",
        ".webmanifest": "application/manifest+json",
    }

    def send_file(self, path, mime=None):
        try:
            path = Path(path)
            with open(path, "rb") as f:
                body = f.read()
            if mime is None:
                mime = self.MIMES.get(path.suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self._entetes_communs(mime, len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_404("Fichier introuvable : %s" % path)

    def servir_statique(self):
        """Sert un fichier du dossier du script (extensions autorisées
        uniquement, et aucune remontée de répertoire possible)."""
        nom = self.path.split("?")[0].lstrip("/")
        if not nom or "/" in nom or "\\" in nom or nom.startswith("."):
            return False
        chemin = (Path(__file__).parent / nom).resolve()
        racine = Path(__file__).parent.resolve()
        if racine not in chemin.parents or not chemin.is_file():
            return False
        if chemin.suffix.lower() not in self.MIMES:
            return False
        self.send_file(chemin)
        return True

    ROUTES_CONNUES = {
        "GET":  ["/", "/index.html", "/cameras.html", "/status", "/logs", "/refresh",
                 "/debug-status", "/proximite-status", "/actualisation-status",
                 "/server-countdown", "/version", "/sante", "/loc/<route localisation.py>"],
        "POST": ["/toggle", "/test-camera", "/debug", "/clear-logs", "/send-telegram",
                 "/proximite-config", "/actualisation-config", "/loc/<route localisation.py>"],
    }

    def send_404(self, detail=""):
        """404 explicite : sans corps, un onglet vide n'apprend rien."""
        log(f"⚠️  404 {self.command} {self.path} — {detail or 'route inconnue'}")
        self.send_json({
            "ok": False,
            "erreur": "route inconnue",
            "detail": detail,
            "methode": self.command,
            "chemin": self.path,
            "routes_disponibles": self.ROUTES_CONNUES.get(self.command, []),
            "version_serveur": VERSION_SERVEUR,
        }, 404)

    def send_500(self, exc):
        import traceback
        trace = traceback.format_exc()
        log(f"❌ Exception sur {self.command} {self.path} : {exc}")
        for ligne in trace.strip().split("\n")[-4:]:
            log(f"   {ligne}")
        try:
            self.send_json({"ok": False, "erreur": str(exc),
                            "chemin": self.path,
                            "version_serveur": VERSION_SERVEUR}, 500)
        except Exception:
            pass   # connexion déjà fermée côté client

    def do_GET(self):
        """Enveloppe : toute exception non rattrapée devenait une connexion
        coupée côté navigateur (ERR_CONNECTION_RESET / ERR_EMPTY_RESPONSE),
        impossible à diagnostiquer. On renvoie désormais un 500 tracé."""
        self._t0 = time.time()
        try:
            self._get_interne()
        except BrokenPipeError:
            pass                       # le client a fermé l'onglet : normal
        except Exception as e:
            self.send_500(e)

    def _get_interne(self):
        global camera_busy
        chemin = self.path.split("?")[0]

        if chemin in ("/", "/index.html", "/cameras.html", "/CameraOnOff.html"):
            dash = fichier_dashboard()
            if dash is None:
                self.send_404("aucun dashboard trouvé dans %s (attendu : %s)"
                              % (Path(__file__).parent, " ou ".join(DASHBOARD_CANDIDATS)))
            else:
                self.send_file(dash, "text/html; charset=utf-8")

        elif chemin == "/version":
            self.send_json({
                "version": VERSION_SERVEUR,
                "historique": [{"version": v, "date": d, "resume": r}
                               for v, d, r in HISTORIQUE_VERSIONS],
            })

        elif chemin == "/sante":
            self.send_json(self._sante())

        elif self.path == "/status":
            with camera_lock:
                statuts = {
                    nom: ("on" if v is True else ("off" if v is False else "unknown"))
                    for nom, v in camera_status.items()
                }
                erreurs = dict(camera_erreur)
                busy = camera_busy
            with camera_lock:
                actions = dict(camera_derniere_action)
                diagnostics = dict(camera_diagnostic)
            self.send_json({"cameras": statuts, "erreurs": erreurs, "busy": busy,
                            "actions": actions, "diagnostics": diagnostics,
                            "version_serveur": VERSION_SERVEUR})

        elif self.path == "/logs":
            with log_lock:
                lines = list(log_buffer)
            self.send_json({"lines": lines})

        elif self.path == "/refresh":
            with camera_lock:
                if camera_busy:
                    self.send_json({"ok": False, "reason": "busy"})
                    return
                camera_busy = True
            threading.Thread(target=lire_tous_les_etats, daemon=True).start()
            self.send_json({"ok": True})

        elif self.path == "/debug-status":
            self.send_json({"debug": debug_mode})

        # ── [AJOUT MODE SERVEUR] ──
        elif self.path == "/proximite-status":
            self.send_json({
                "mode_serveur":  proximite_mode_serveur,
                "intervalle_s":  proximite_intervalle_s,
                "cameras":       proximite_cameras,
            })
        elif self.path == "/actualisation-status":
            self.send_json({
                "mode_serveur": actualisation_mode_serveur,
                "intervalle_s": actualisation_intervalle_s,
            })

        elif self.path == "/server-countdown":
            now = time.time()
            # Proximité
            if proximite_mode_serveur and _prox_timer_start is not None:
                prox_total   = proximite_intervalle_s
                prox_elapsed = now - _prox_timer_start
                prox_restant = max(0, int(prox_total - prox_elapsed))
            else:
                prox_total   = proximite_intervalle_s
                prox_restant = None

            # Actualisation
            if actualisation_mode_serveur and _actu_timer_start is not None:
                actu_total   = actualisation_intervalle_s
                actu_elapsed = now - _actu_timer_start
                actu_restant = max(0, int(actu_total - actu_elapsed))
            else:
                actu_total   = actualisation_intervalle_s
                actu_restant = None

            self.send_json({
                "proximite": {
                    "actif":    proximite_mode_serveur,
                    "restant_s": prox_restant,
                    "total_s":  prox_total,
                },
                "actualisation": {
                    "actif":    actualisation_mode_serveur,
                    "restant_s": actu_restant,
                    "total_s":  actu_total,
                },
            })
        # ── [FIN AJOUT] ──

        # ── [AJOUT PROXY LOCALISATION] : relais GET vers localisation.py (port 8282) ──
        elif self.path.startswith("/loc/"):
            cible = "http://localhost:8282" + self.path[len("/loc"):]
            try:
                with urllib.request.urlopen(cible, timeout=10) as r:
                    corps = r.read()
                    self.send_response(r.status)
                    self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(corps)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(corps)
            except urllib.error.URLError as e:
                self.send_json({"ok": False, "erreur": str(e)}, 502)
        # ── [FIN AJOUT PROXY] ──

        # Tout autre fichier du dossier (leaflet local, icône, manifeste…)
        elif self.servir_statique():
            pass

        else:
            self.send_404()

    # ═══════════════════════════════════════════════════════════
    #  BILAN DE SANTÉ — une seule requête pour tout savoir
    # ═══════════════════════════════════════════════════════════
    def _sante(self):
        dossier = Path(__file__).parent
        dash = fichier_dashboard()

        # localisation.py (port 8282) répond-il ?
        loc_etat, loc_detail = "injoignable", ""
        try:
            with urllib.request.urlopen("http://localhost:8282/distances", timeout=3) as r:
                loc_etat = "ok" if r.status == 200 else f"HTTP {r.status}"
        except Exception as e:
            loc_detail = str(e)

        fichiers = {}
        for nom in ("localisationParam.json", "historique_positions.json",
                    "coordonnees.txt", "historique_cameras.json",
                    "localisation.py", "cameraControl.py"):
            p = dossier / nom
            fichiers[nom] = {"present": p.exists(),
                             "octets": (p.stat().st_size if p.exists() else 0)}
        fichiers["chrome_profile/"] = {"present": (dossier / "chrome_profile").is_dir(),
                                       "octets": 0}

        with camera_lock:
            etats = {n: ("on" if v is True else ("off" if v is False else "unknown"))
                     for n, v in camera_status.items()}
            occupe = camera_busy

        uptime = int(time.time() - DEMARRAGE_TS)
        return {
            "ok": True,
            "version_serveur": VERSION_SERVEUR,
            "pid": os.getpid(),
            "port": PORT,
            "schema": SCHEMA,
            "https": HTTPS_ACTIF,
            "uptime_s": uptime,
            "uptime_lisible": f"{uptime // 3600} h {(uptime % 3600) // 60} min {uptime % 60} s",
            "dossier": str(dossier),
            "dashboard_servi": (dash.name if dash else None),
            "cameras": etats,
            "camera_busy": occupe,
            "file_attente": toggle_queue.qsize() if 'toggle_queue' in globals() else None,
            "threads_actifs": threading.active_count(),
            "debug_mode": debug_mode,
            "mode_proximite": proximite_mode_serveur,
            "mode_actualisation": actualisation_mode_serveur,
            "localisation_py": {"etat": loc_etat, "detail": loc_detail, "port": 8282},
            "fichiers": fichiers,
            "lignes_log": len(log_buffer),
        }

    def do_POST(self):
        self._t0 = time.time()
        try:
            self._post_interne()
        except BrokenPipeError:
            pass
        except Exception as e:
            self.send_500(e)

    def _post_interne(self):
        global camera_busy, debug_mode
        if self.path == "/send-telegram":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            texte  = body.get("message", "").strip()
            
            if texte:
                log(f"📩 Requête d'envoi Telegram reçue : '{texte}'")
                # [FIX] markdown=False : ce texte peut venir de la zone de saisie libre
                # du dashboard OU des messages de changement d'état (checkStateChanges côté
                # JS) — ni l'un ni l'autre n'est garanti "Markdown-safe".
                envoyer_telegram(texte, markdown=False)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "reason": "empty_message"}, 400)
        elif self.path == "/debug":
            debug_mode = not debug_mode
            etat = "activé" if debug_mode else "désactivé"
            log(f"🔬 Mode debug {etat}")
            _sauvegarder_preferences_serveur({"debug": debug_mode})
            self.send_json({"debug": debug_mode})

        elif self.path == "/clear-logs":
            with log_lock:
                log_buffer.clear()
            self.send_json({"ok": True})

        elif self.path == "/toggle":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            nom    = body.get("camera")
            cible  = body.get("target")
            if nom not in NOMS_CAMERAS or cible not in ("on", "off"):
                self.send_json({"ok": False, "reason": "invalid"}, 400)
                return
            # [AJOUT] mémoriser la source pour _check_state_changes_serveur
            _last_change_source[nom] = body.get("source", "manual")
            # [FIN AJOUT]
            toggle_queue.put((nom, cible == "on"))
            self.send_json({"ok": True, "queued": toggle_queue.qsize()})

        # ── [AJOUT TEST CAMÉRA] : diagnostic d'une caméra (bouton 🩺 Test) ──
        elif self.path == "/test-camera":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            nom    = body.get("camera")
            if nom not in NOMS_CAMERAS:
                self.send_json({"ok": False, "reason": "invalid"}, 400)
                return
            with camera_lock:
                if camera_busy:
                    self.send_json({"ok": False, "reason": "busy"})
                    return
                camera_busy = True
            threading.Thread(target=tester_camera, args=(nom,), daemon=True).start()
            self.send_json({"ok": True})
        # ── [FIN AJOUT TEST CAMÉRA] ──

        # ── [AJOUT MODE SERVEUR] ──
        elif self.path == "/proximite-config":
            global proximite_mode_serveur, proximite_intervalle_s, proximite_cameras
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            mode_serveur      = bool(body.get("mode_serveur", False))
            intervalle_min    = float(body.get("intervalle_min", 2))
            cameras           = [n for n in body.get("cameras", []) if n in NOMS_CAMERAS]

            proximite_intervalle_s = max(6, round(intervalle_min * 60))
            proximite_cameras      = cameras

            if mode_serveur and not proximite_mode_serveur:
                # Activation du mode serveur
                proximite_mode_serveur = True
                _planifier_tick_proximite()
                log(f"🏠 [SERVEUR] Mode proximité SERVEUR activé — intervalle={proximite_intervalle_s}s — caméras={cameras}")
            elif not mode_serveur and proximite_mode_serveur:
                # Désactivation : retour au mode HTML
                proximite_mode_serveur = False
                _arreter_timer_proximite()
                log("🏠 [SERVEUR] Mode proximité SERVEUR désactivé — retour mode HTML")
            else:
                # Mise à jour de la config sans changement de mode
                if proximite_mode_serveur:
                    _planifier_tick_proximite()   # relancer avec le nouvel intervalle
                log(f"🏠 [SERVEUR] Config proximité mise à jour — intervalle={proximite_intervalle_s}s — caméras={cameras}")

            self.send_json({
                "ok":           True,
                "mode_serveur": proximite_mode_serveur,
                "intervalle_s": proximite_intervalle_s,
                "cameras":      proximite_cameras,
            })
            _sauvegarder_preferences_serveur({
                "proximite": {
                    "mode_serveur": proximite_mode_serveur,
                    "intervalle_s": proximite_intervalle_s,
                    "cameras":      proximite_cameras,
                }
            })

        elif self.path == "/actualisation-config":
            global actualisation_mode_serveur, actualisation_intervalle_s
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            mode_serveur   = bool(body.get("mode_serveur", False))
            intervalle_min = float(body.get("intervalle_min", 2))

            actualisation_intervalle_s = max(6, round(intervalle_min * 60))

            if mode_serveur and not actualisation_mode_serveur:
                actualisation_mode_serveur = True
                _planifier_tick_actualisation()
                log(f"🔄 [SERVEUR] Mode actualisation SERVEUR activé — intervalle={actualisation_intervalle_s}s")
            elif not mode_serveur and actualisation_mode_serveur:
                actualisation_mode_serveur = False
                _arreter_timer_actualisation()
                log("🔄 [SERVEUR] Mode actualisation SERVEUR désactivé — retour mode HTML")
            else:
                if actualisation_mode_serveur:
                    _planifier_tick_actualisation()   # relancer avec le nouvel intervalle
                log(f"🔄 [SERVEUR] Config actualisation mise à jour — intervalle={actualisation_intervalle_s}s")

            self.send_json({
                "ok":           True,
                "mode_serveur": actualisation_mode_serveur,
                "intervalle_s": actualisation_intervalle_s,
            })
            _sauvegarder_preferences_serveur({
                "actualisation": {
                    "mode_serveur": actualisation_mode_serveur,
                    "intervalle_s": actualisation_intervalle_s,
                }
            })
        # ── [FIN AJOUT] ──

        # ── [AJOUT PROXY LOCALISATION] : relais POST vers localisation.py (port 8282) ──
        elif self.path.startswith("/loc/"):
            cible = "http://localhost:8282" + self.path[len("/loc"):]
            length = int(self.headers.get("Content-Length", 0))
            corps_in = self.rfile.read(length) if length else b""
            requete = urllib.request.Request(
                cible, data=corps_in, method="POST",
                headers={"Content-Type": self.headers.get("Content-Type", "application/json")}
            )
            try:
                with urllib.request.urlopen(requete, timeout=10) as r:
                    corps = r.read()
                    self.send_response(r.status)
                    self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(corps)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(corps)
            except urllib.error.URLError as e:
                self.send_json({"ok": False, "erreur": str(e)}, 502)
        # ── [FIN AJOUT PROXY] ──

        else:
            self.send_404()

    def do_OPTIONS(self):
        """Répond aux preflight CORS (identique à localisation.py)."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

# ═══════════════════════════════════════════════════════════════
# FORK → BACKGROUND
# ═══════════════════════════════════════════════════════════════

def daemoniser():
    """
    Double-fork UNIX : détache complètement le process du terminal.
    Le parent affiche l'URL et quitte. L'enfant continue en background.
    """
    pid = os.fork()
    if pid > 0:
        # Parent : affiche l'URL et quitte immédiatement
        dash = fichier_dashboard()
        print(f"\n{'═'*62}")
        print(f"  📷  Nest Camera Dashboard — serveur v{VERSION_SERVEUR}")
        print(f"  🔐  {'HTTPS (certificat Tailscale)' if HTTPS_ACTIF else 'HTTP non chiffré'}")
        print(f"  🌐  {SCHEMA}://localhost:{PORT}/")
        try:
            import socket
            hostname = socket.gethostname().split(".")[0]
            print(f"  🌐  {SCHEMA}://{hostname}.local:{PORT}/")
        except Exception:
            pass
        if HTTPS_ACTIF:
            print(f"  🌐  https://{CERT_FILE.stem}:{PORT}/   ← via Tailscale")
            print(f"  ⚠️   Le serveur écoute en TLS : une URL http:// donnera")
            print(f"      ERR_CONNECTION_RESET. Utilisez bien https://")
        print(f"  📄  Dashboard servi : {dash.name if dash else '❌ AUCUN (' + ' ou '.join(DASHBOARD_CANDIDATS) + ' attendu)'}")
        print(f"  🩺  Bilan de santé : {SCHEMA}://localhost:{PORT}/sante")
        print(f"  📋  Logs : page HTML, ou {SCHEMA}://localhost:{PORT}/logs")
        print(f"  ⚙️   PID : {pid}   ·   arrêt : kill {pid}")
        print(f"{'═'*62}\n")
        sys.exit(0)

    # Premier enfant : créer une nouvelle session (détacher du terminal)
    os.setsid()

    # Deuxième fork : éviter que le process redevienne leader de session
    pid2 = os.fork()
    if pid2 > 0:
        sys.exit(0)

    # Petit-enfant : c'est lui qui tourne en background
    # Rediriger stdin/stdout/stderr vers /dev/null
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open(os.devnull, "w") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ── Options de ligne de commande ─────────────────────────────
    #   --version      affiche la version et quitte
    #   --foreground   reste au premier plan (logs dans le terminal, Ctrl-C
    #                  pour arrêter) : indispensable pour déboguer
    #   --port N       écoute sur un autre port
    args = sys.argv[1:]
    if "--version" in args or "-v" in args:
        print(f"cameraServer.py v{VERSION_SERVEUR}")
        for v, d, r in HISTORIQUE_VERSIONS:
            print(f"  v{v} ({d}) — {r}")
        sys.exit(0)
    if "--aide" in args or "--help" in args or "-h" in args:
        print("Usage : python3.13 cameraServer.py [--foreground] [--port N] [--version]")
        sys.exit(0)
    PREMIER_PLAN = "--foreground" in args or "--fg" in args
    if "--port" in args:
        try:
            PORT = int(args[args.index("--port") + 1])
        except (IndexError, ValueError):
            print("❌ --port attend un numéro de port"); sys.exit(1)

    profil_dir = Path(__file__).parent / "chrome_profile"
    if not profil_dir.exists():
        print("❌ Aucune session Google trouvée.")
        print("   Lance d'abord : python3.13 cameraControl.py --setup")
        sys.exit(1)

    if fichier_dashboard() is None:
        print(f"⚠️  Aucun dashboard trouvé dans {Path(__file__).parent}")
        print(f"   Attendu : {' ou '.join(DASHBOARD_CANDIDATS)}")
        print("   Le serveur démarre quand même : l'API répondra, mais la racine renverra 404.")

    # Passer en background (le terminal est libéré immédiatement),
    # sauf en --foreground où l'on garde les logs sous les yeux.
    if PREMIER_PLAN:
        print(f"\n{'═'*62}")
        print(f"  📷  Nest Camera Dashboard v{VERSION_SERVEUR} — PREMIER PLAN")
        print(f"  🌐  {SCHEMA}://localhost:{PORT}/   ·   Ctrl-C pour arrêter")
        print(f"{'═'*62}\n")
    else:
        daemoniser()

    # ── À partir d'ici : process background uniquement ──

    # Worker de file d'attente des basculements
    threading.Thread(target=toggle_worker, daemon=True).start()

    # Lecture initiale des états
    camera_busy = True
    threading.Thread(target=lire_tous_les_etats, daemon=True).start()

    # ── [AJOUT] ── Restauration des modes serveur (proximité / actualisation / debug)
    # localisation.py (port 8282) peut démarrer en parallèle et ne pas être
    # encore prêt : on retente quelques secondes en arrière-plan.
    def _restaurer_modes_serveur_avec_retry():
        for tentative in range(6):
            prefs = _charger_preferences_serveur()
            if prefs:
                _restaurer_modes_serveur()
                return
            time.sleep(5)
        log("⚠️ Préférences serveur non restaurées (localisation.py indisponible)")
    threading.Thread(target=_restaurer_modes_serveur_avec_retry, daemon=True).start()
    # ── [FIN AJOUT] ──

    # Démarrer le serveur HTTP ou HTTPS selon la disponibilité du certificat Tailscale
    # [FIX] ThreadingHTTPServer au lieu de HTTPServer : l'ancien serveur mono-thread
    # traitait une requête à la fois. Un appel lent (ex: /loc/* qui attend jusqu'à 10s
    # une réponse de localisation.py, elle-même parfois bloquée sur GitHub) gelait alors
    # TOUTES les autres requêtes, y compris le polling /status et /logs du dashboard.
    # L'état partagé (camera_status, camera_busy, log_buffer...) est déjà protégé par
    # des threading.Lock() dédiés, donc le passage au multi-thread est sûr.
    if HTTPS_ACTIF:
        # Créer le contexte SSL AVANT d'instancier HTTPServer
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))

        class HTTPSServer(ThreadingHTTPServer):
            def get_request(self):
                (sock, addr) = super().get_request()
                return (ctx.wrap_socket(sock, server_side=True), addr)

        server = HTTPSServer(("0.0.0.0", PORT), Handler)
        log(f"🔒 HTTPS activé avec certificat Tailscale (multi-thread)")
    else:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        log(f"⚠️ Certificat Tailscale non trouvé — HTTP non chiffré (multi-thread)")

    log(f"🚀 cameraServer.py v{VERSION_SERVEUR} démarré — {SCHEMA}://localhost:{PORT}/ "
        f"(PID {os.getpid()}, dashboard : {fichier_dashboard().name if fichier_dashboard() else 'aucun'})")

    # ── Arrêt propre : sans cela, un kill laissait le port occupé quelques
    #    secondes et la relance échouait avec « Address already in use ». ──
    import signal

    def _arret(signum, frame):
        log(f"🛑 Signal {signum} reçu — arrêt du serveur")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _arret)
        except (ValueError, OSError):
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("🛑 Interruption clavier — arrêt du serveur")
    except Exception as e:
        log(f"❌ Serveur arrêté sur erreur : {e}")
    finally:
        try:
            server.server_close()
            log("✅ Port libéré proprement")
        except Exception:
            pass
