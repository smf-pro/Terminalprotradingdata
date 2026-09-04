# -*- coding: utf-8 -*-
"""
Script d'extraction - RateProbability.com (version cloud)
------------------------------------------------------------------------------
Version adaptée pour tourner sur GitHub Actions (cron).

Différences par rapport à la version locale (Implicite.py) :
- Pas de boucle infinie : un seul passage (single-pass), c'est GitHub Actions
  qui se charge de relancer le script périodiquement.
- Pas de fichiers .txt locaux datés (DD-MM-YYYY-rates-*.txt) : tout est
  écrit et vérifié dans Firestore.
- Deux collections :
    "rates_comparatif"        -> historique du tableau comparatif (une
                                  entrée par relevé où les données ont
                                  changé depuis le relevé précédent)
    "rates_comparatif_latest" -> un seul document, toujours écrasé, qui
                                  contient le dernier relevé + son hash
                                  (sert de référence pour la dédup)
    "rates_detail"            -> historique du détail par banque (une
                                  entrée par banque et par relevé où les
                                  données ont changé)
    "rates_detail_latest"     -> un document par banque (id = slug),
                                  toujours écrasé, sert de référence pour
                                  la dédup
- Dédup : comme le site ne publie ses maj que 1 à 3x/jour alors que le
  cron peut tourner plus souvent, on calcule un hash SHA256 du contenu
  (tableau comparatif, ou détail d'une banque) et on ne crée une NOUVELLE
  entrée d'historique QUE si ce hash diffère du dernier connu. Le
  document "latest" correspondant est lui toujours mis à jour, pour
  savoir à quand remonte la dernière vérification même si rien n'a
  changé.

⚠️ Mêmes remarques que la version locale concernant le rendu JS de
certains encarts, et le respect des CGU / robots.txt de RateProbability.

Dépendances (voir requirements.txt) :
    requests, beautifulsoup4, lxml, firebase-admin
"""

import os
import time
import json
import hashlib
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore

# ---------- CONFIGURATION ----------
URL_ACCUEIL = "https://rateprobability.com/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
DELAI_ENTRE_REQUETES = 3  # secondes, pause polie entre deux requêtes (banques)

COLLECTION_COMPARATIF = "rates_comparatif"
COLLECTION_COMPARATIF_LATEST = "rates_comparatif_latest"
COLLECTION_DETAIL = "rates_detail"
COLLECTION_DETAIL_LATEST = "rates_detail_latest"

# Banques suivies : slug utilisé dans l'URL -> nom affiché
BANQUES = {
    "fed":  "Federal Reserve (Fed)",
    "ecb":  "European Central Bank (ECB)",
    "boj":  "Bank of Japan (BoJ)",
    "boe":  "Bank of England (BoE)",
    "boc":  "Bank of Canada (BoC)",
    "rba":  "Reserve Bank of Australia (RBA)",
    "rbnz": "Reserve Bank of New Zealand (RBNZ)",
    "snb":  "Swiss National Bank (SNB)",
    "srb":  "Riksbank",
    "rbi":  "Reserve Bank of India (RBI)",
}

# Mots-clés utilisés pour repérer les bons tableaux dans le HTML, sans
# dépendre d'une classe CSS précise (voir avertissement dans la version locale)
MOTS_CLES_TABLEAU_COMPARATIF = ["bank", "policy rate", "probability"]
MOTS_CLES_TABLEAU_DETAIL = ["meeting", "implied rate", "probability"]


# ---------- INITIALISATION FIREBASE ----------

def init_firestore():
    if not firebase_admin._apps:
        chemin_credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
        cred = credentials.Certificate(chemin_credentials)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def hash_contenu(objet):
    """Hash SHA256 stable d'une structure de données (liste de dicts),
    en sérialisant en JSON trié pour que l'ordre des clés n'influence
    pas le résultat."""
    brut = json.dumps(objet, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


# ---------- PARSING (identique à la version locale) ----------

def trouver_tableau(soup, mots_cles):
    meilleur_tableau = None
    meilleur_score = 0

    for table in soup.find_all("table"):
        entetes = table.find_all("th")
        if not entetes:
            premiere_ligne = table.find("tr")
            entetes = premiere_ligne.find_all(["td", "th"]) if premiere_ligne else []

        texte_entetes = " | ".join(e.get_text(strip=True).lower() for e in entetes)
        score = sum(1 for mot in mots_cles if mot in texte_entetes)

        if score > meilleur_score:
            meilleur_score = score
            meilleur_tableau = table

    if meilleur_score == 0:
        return None
    return meilleur_tableau


def extraire_lignes_tableau(table):
    lignes_html = table.find_all("tr")
    if not lignes_html:
        return []

    cellules_entete = lignes_html[0].find_all(["th", "td"])
    entetes = [c.get_text(strip=True) for c in cellules_entete]
    if not entetes:
        return []

    resultats = []
    for ligne in lignes_html[1:]:
        cellules = ligne.find_all(["td", "th"])
        if not cellules or len(cellules) < 2:
            continue
        valeurs = [c.get_text(strip=True) for c in cellules]
        if len(valeurs) < len(entetes):
            valeurs += [""] * (len(entetes) - len(valeurs))
        ligne_dict = dict(zip(entetes, valeurs[:len(entetes)]))
        if any(v.strip() and v.strip() != "—" for v in ligne_dict.values()):
            resultats.append(ligne_dict)

    return resultats


def recuperer_page(url):
    reponse = requests.get(url, headers=HEADERS, timeout=15)
    reponse.raise_for_status()
    return BeautifulSoup(reponse.text, "html.parser")


# ---------- RÉCUPÉRATION DES DONNÉES (identique à la version locale) ----------

def recuperer_comparatif():
    soup = recuperer_page(URL_ACCUEIL)
    table = trouver_tableau(soup, MOTS_CLES_TABLEAU_COMPARATIF)
    if table is None:
        return []
    return extraire_lignes_tableau(table)


def recuperer_detail_banque(slug):
    url = f"https://rateprobability.com/{slug}"
    soup = recuperer_page(url)
    table = trouver_tableau(soup, MOTS_CLES_TABLEAU_DETAIL)
    if table is None:
        return []
    return extraire_lignes_tableau(table)


# ---------- SAUVEGARDE FIRESTORE ----------

def sauvegarder_comparatif(db, lignes, maintenant):
    """Écrit dans Firestore uniquement si le contenu a changé depuis le
    dernier relevé connu (dédup par hash), mais met toujours à jour le
    document 'latest' pour tracer la date de dernière vérification."""
    nouveau_hash = hash_contenu(lignes)
    doc_latest_ref = db.collection(COLLECTION_COMPARATIF_LATEST).document("current")
    doc_latest = doc_latest_ref.get()
    ancien_hash = doc_latest.to_dict().get("hash") if doc_latest.exists else None

    a_change = nouveau_hash != ancien_hash

    if a_change:
        db.collection(COLLECTION_COMPARATIF).document(nouveau_hash).set({
            "donnees": lignes,
            "date_relevé": maintenant,
            "nb_lignes": len(lignes),
        })
        print(f"Comparatif : changement détecté, nouvelle entrée d'historique ({len(lignes)} ligne(s)).")
    else:
        print("Comparatif : aucun changement depuis le dernier relevé.")

    doc_latest_ref.set({
        "donnees": lignes,
        "hash": nouveau_hash,
        "date_derniere_verification": maintenant,
        "a_change": a_change,
    })

    return a_change


def sauvegarder_detail_banque(db, slug, nom_banque, lignes, maintenant):
    nouveau_hash = hash_contenu(lignes)
    doc_latest_ref = db.collection(COLLECTION_DETAIL_LATEST).document(slug)
    doc_latest = doc_latest_ref.get()
    ancien_hash = doc_latest.to_dict().get("hash") if doc_latest.exists else None

    a_change = nouveau_hash != ancien_hash

    if a_change:
        doc_id = f"{slug}_{nouveau_hash}"
        db.collection(COLLECTION_DETAIL).document(doc_id).set({
            "banque_slug": slug,
            "banque_nom": nom_banque,
            "donnees": lignes,
            "date_relevé": maintenant,
            "nb_reunions": len(lignes),
        })
        print(f"{nom_banque} : changement détecté, nouvelle entrée d'historique ({len(lignes)} réunion(s)).")
    else:
        print(f"{nom_banque} : aucun changement depuis le dernier relevé.")

    doc_latest_ref.set({
        "banque_slug": slug,
        "banque_nom": nom_banque,
        "donnees": lignes,
        "hash": nouveau_hash,
        "date_derniere_verification": maintenant,
        "a_change": a_change,
    })

    return a_change


# ---------- PROGRAMME PRINCIPAL (single-pass) ----------

def cycle():
    db = init_firestore()
    maintenant = datetime.now(timezone.utc)

    # 1) Tableau comparatif (page d'accueil)
    try:
        lignes_comparatif = recuperer_comparatif()
    except requests.exceptions.RequestException as e:
        print(f"Erreur lors de la récupération de la page d'accueil : {e}")
        lignes_comparatif = []

    print(f"Comparatif : {len(lignes_comparatif)} ligne(s) trouvée(s) sur la page.")
    sauvegarder_comparatif(db, lignes_comparatif, maintenant)

    # 2) Détail par banque
    changements = 0
    for slug, nom_banque in BANQUES.items():
        try:
            lignes = recuperer_detail_banque(slug)
            print(f"{nom_banque} : {len(lignes)} réunion(s) trouvée(s) sur la page.")
        except requests.exceptions.RequestException as e:
            print(f"Erreur lors de la récupération de {nom_banque} ({slug}) : {e}")
            lignes = []

        if sauvegarder_detail_banque(db, slug, nom_banque, lignes, maintenant):
            changements += 1

        time.sleep(DELAI_ENTRE_REQUETES)  # pause polie entre chaque banque

    print(f"\nTerminé. {changements} banque(s) mise(s) à jour dans l'historique.")


if __name__ == "__main__":
    cycle()
