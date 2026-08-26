#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gaiverland-dl — téléchargeur LOCAL de la radio Gaiverland.

Pourquoi : le serveur est bridé par YouTube (quota quotidien, cookies qui brûlent).
Ta machine personnelle ne l'est pas. Ce script récupère la file d'attente de la
radio, télécharge les titres ICI, puis les dépose sur le serveur qui les range,
les analyse et les met en rotation tout seul.

Usage :
    python3 gaiverland-dl.py               # traite toute la file
    python3 gaiverland-dl.py --limit 10    # seulement 10 titres
    python3 gaiverland-dl.py --liste       # montre la file sans rien télécharger

Configuration : fichier config.json à côté de ce script (créé par l'installateur) :
    {"hote": "https://gaiverland.gaiver-it.fr", "jeton": "<jeton de la régie>"}

Prérequis : python3, yt-dlp (module python), ffmpeg dans le PATH.
L'installateur (install.sh / install.ps1) vérifie et installe tout ça.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
# Le même filtre anti-clip que le serveur : le rip du clip officiel gagne sinon la
# recherche, avec l'intro/outro de la vidéo (le coup du match de tennis de « Hello »).
FILTRE = ("duration>=45 & duration<=900 & "
          "title!~=(?i)(official.?music.?video|official.?video|music.?video|album teaser)")


def config():
    chemin = os.path.join(ICI, "config.json")
    if not os.path.isfile(chemin):
        sys.exit("config.json introuvable — lance d'abord l'installateur (install.sh / install.ps1)")
    with open(chemin, encoding="utf-8") as f:
        c = json.load(f)
    hote = c.get("hote", "").rstrip("/")
    jeton = c.get("jeton", "")
    if not hote or not jeton:
        sys.exit("config.json incomplet : il faut 'hote' et 'jeton'")
    return hote, jeton


def prerequis():
    if shutil.which("ffmpeg") is None:
        conseil = {"darwin": "brew install ffmpeg",
                   "win32": "winget install Gyan.FFmpeg (puis rouvre le terminal)"}
        sys.exit("ffmpeg introuvable — installe-le : "
                 + conseil.get(sys.platform, "sudo apt install ffmpeg"))
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        sys.exit("yt-dlp manquant — lance :  %s -m pip install --user -U yt-dlp"
                 % os.path.basename(sys.executable))


def api(url, corps=None, entetes=None):
    req = urllib.request.Request(url, data=corps, headers=entetes or {})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def telecharger(cle, dossier_tmp):
    """Deux passes, comme le serveur : filtre anti-clip d'abord, repli sans filtre."""
    gabarit = os.path.join(dossier_tmp, "%(uploader)s - %(title)s.%(ext)s")
    base = [sys.executable, "-m", "yt_dlp", "-f", "bestaudio", "-x",
            "--audio-format", "mp3", "--audio-quality", "0",
            "--max-downloads", "1", "--no-playlist", "--no-warnings", "-q",
            "-o", gabarit]
    for args in ([*base, "--match-filter", FILTRE], base):
        r = subprocess.run([*args, f"ytsearch8:{cle} audio"],
                           capture_output=True, text=True)
        # 101 = --max-downloads atteint = succès (même convention que le serveur)
        if r.returncode in (0, 101):
            mp3 = [f for f in os.listdir(dossier_tmp) if f.lower().endswith(".mp3")]
            if mp3:
                return os.path.join(dossier_tmp, mp3[0])
    return None


def main():
    ap = argparse.ArgumentParser(description="Téléchargeur local Gaiverland")
    ap.add_argument("--limit", type=int, default=0, help="nombre max de titres (0 = tout)")
    ap.add_argument("--liste", action="store_true", help="afficher la file et sortir")
    opts = ap.parse_args()

    prerequis()
    hote, jeton = config()

    d = api(f"{hote}/api/regie/dl-local/attente?k={urllib.parse.quote(jeton)}")
    attente = d.get("attente", [])
    print(f"File d'attente du serveur : {len(attente)} titre(s)")
    if opts.liste or not attente:
        for i, t in enumerate(attente, 1):
            print(f"  {i:3d}. [{t['dossier']}] {t['cle']}")
        return
    if opts.limit:
        attente = attente[: opts.limit]

    ok = ko = 0
    for i, t in enumerate(attente, 1):
        cle, dossier = t["cle"], t["dossier"]
        print(f"[{i}/{len(attente)}] {cle} … ", end="", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            fichier = telecharger(cle, tmp)
            if not fichier:
                print("échec du téléchargement")
                ko += 1
                continue
            nom = os.path.basename(fichier)
            with open(fichier, "rb") as f:
                corps = f.read()
            # le depot marque la file par le TITRE d'origine (cle = requete de recherche,
            # parfois affinee cote serveur — les deux peuvent differer)
            params = urllib.parse.urlencode(
                {"k": jeton, "type": t["type"], "cle": t.get("titre", cle),
                 "dossier": dossier, "nom": nom})
            try:
                rep = api(f"{hote}/api/regie/dl-local/depot?{params}", corps=corps,
                          entetes={"Content-Type": "application/octet-stream"})
                if rep.get("ok"):
                    print(f"✓ déposé ({len(corps)//1024} ko → music/{dossier}/)")
                    ok += 1
                else:
                    print("refusé par le serveur :", rep)
                    ko += 1
            except Exception as e:
                print("dépôt impossible :", str(e)[:120])
                ko += 1

    print(f"\nBilan : {ok} déposé(s), {ko} échec(s).")
    if ok:
        print("Le serveur range et analyse tout seul — les titres entrent en rotation "
              "d'eux-mêmes (scan ~1 min, analyse ~2 min par titre, rotation toutes les 30 min).")


if __name__ == "__main__":
    main()
