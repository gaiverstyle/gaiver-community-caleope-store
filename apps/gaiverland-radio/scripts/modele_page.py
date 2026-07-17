"""
Page « Notre modèle » — Tomorrowland (servie par gcs-web sur /modele).

RÈGLE NON NÉGOCIABLE (chef + Cassy) : on EMBARQUE / on LIE l'OFFICIEL, on ne
RÉ-HÉBERGE JAMAIS. Le live vient d'un <iframe> YouTube officiel → l'image ET le son
restent servis par YouTube (pubs + attribution + vues chez EUX). Aucun proxy, aucun
re-stream via nos URLs ou AzuraCast. Pas de logo/branding détourné, aucun sous-entendu
de partenariat. Attribution + « non affilié » affichés clairement.

Config (lue au moment de la requête, cf gcs_web) :
- TOMORROWLAND_ACTIVE : page saisonnière (masquée hors événement).
- TOMORROWLAND_YT     : cible du live — URL YouTube, ID vidéo (11 car.) ou ID chaîne (UC…).
                        Vide → repli « Regarder le live officiel » (lien, pas d'embed).
"""
import re, json

# Liens OFFICIELS (on POINTE, on ne réhéberge pas). Le suffixe /live d'une chaîne
# redirige vers son direct courant → repli fiable sans ID vidéo à maintenir.
OFFICIAL_CHANNEL = "https://www.youtube.com/c/tomorrowland"
OFFICIAL_LIVE    = "https://www.youtube.com/c/tomorrowland/live"
OFFICIAL_SITE    = "https://www.tomorrowland.com/"


def yt_embed_src(raw: str):
    """URL d'embed YouTube à partir d'une URL / ID vidéo / ID chaîne. None si rien d'exploitable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("UC") and len(raw) >= 20 and "/" not in raw and " " not in raw:
        return "https://www.youtube.com/embed/live_stream?channel=" + raw
    m = re.search(r"(?:v=|youtu\.be/|/live/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", raw)
    vid = m.group(1) if m else (raw if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw) else None)
    return "https://www.youtube.com/embed/" + vid if vid else None


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _live_block(streams):
    """streams = [(label, embed_src), …]. MENU de scènes + UN seul lecteur (on choisit).
    Rien d'embarquable → repli lien officiel."""
    valid = [(lbl, src) for lbl, src in streams if src]
    if not valid:
        return (
            '<div class="offline">'
            '<p>Les directs officiels s\'afficheront ici pendant leurs horaires de stream.</p>'
            '<a class="btn" href="' + OFFICIAL_LIVE + '" target="_blank" rel="noopener noreferrer">'
            '▶ Regarder le live officiel sur YouTube</a></div>'
        )
    # Boutons de sélection (comme le sélecteur de stations de l'accueil).
    tabs = "".join(
        '<button class="ms-tab' + (' on' if i == 0 else '') + '" type="button" '
        'onclick="msPick(' + str(i) + ')">' + _esc(lbl) + '</button>'
        for i, (lbl, _) in enumerate(valid))
    data = json.dumps([{"l": lbl, "s": src} for lbl, src in valid])
    # UN iframe, dont on change juste la source au clic → un seul flux chargé à la fois.
    return (
        '<div class="ms-picker">' + tabs + '</div>'
        '<div class="frame"><iframe id="ms-frame" src="' + valid[0][1] + '"'
        ' title="Diffusion officielle Tomorrowland (YouTube)" loading="lazy"'
        ' allow="encrypted-media; picture-in-picture; fullscreen" allowfullscreen'
        ' referrerpolicy="strict-origin-when-cross-origin"></iframe></div>'
        '<p class="cap">Diffusion officielle Tomorrowland via YouTube — pubs et vues chez eux. '
        'Chaque scène s\'affiche pendant ses horaires de stream. Choisis la scène ci-dessus.</p>'
        '<script>var MS=' + data + ';function msPick(i){'
        'var f=document.getElementById("ms-frame");if(f)f.src=MS[i].s;'
        'var b=document.querySelectorAll(".ms-tab");for(var j=0;j<b.length;j++)'
        'b[j].classList.toggle("on",j===i);}</script>'
    )


_CSS = """
:root{--cream:#fff4e6}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font-family:Helvetica,Arial,sans-serif;color:var(--cream);
 background:linear-gradient(175deg,#191036 0%,#3d1d5c 30%,#8b5cf6 55%,#ff5e7a 78%,#ff9a5a 100%);
 background-attachment:fixed;padding:40px 18px 64px}
.wrap{max-width:900px;margin:0 auto}
a{color:#ffd7a8}
.logo{font-family:Georgia,serif;font-weight:700;font-size:40px;letter-spacing:1px;text-align:center;margin:0;
 background:linear-gradient(90deg,#ffd29a,#ff8fa3,#c9b6ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{text-align:center;font-size:13px;letter-spacing:5px;text-transform:uppercase;opacity:.8;margin:6px 0 8px}
.dates{text-align:center;font-size:13px;opacity:.75;margin-bottom:22px}
.disclaim{background:rgba(0,0,0,.22);border:1px solid rgba(255,244,230,.22);border-radius:12px;
 padding:12px 16px;font-size:12.5px;line-height:1.5;opacity:.92;margin-bottom:22px}
.card{background:rgba(255,244,230,.08);border:1px solid rgba(255,244,230,.22);border-radius:16px;
 padding:22px;margin-bottom:20px}
.card h2{margin:0 0 12px;font-size:19px}
.ms-picker{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.ms-tab{background:rgba(255,244,230,.1);border:1px solid rgba(255,244,230,.25);color:var(--cream);
 border-radius:999px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;transition:.15s}
.ms-tab:hover{background:rgba(255,244,230,.2)}
.ms-tab.on{background:linear-gradient(90deg,#ff8a3d,#ff3b5c);border-color:transparent;
 box-shadow:0 2px 10px rgba(255,59,92,.35)}
.frame{position:relative;padding-bottom:56.25%;height:0;border-radius:12px;overflow:hidden;background:#000}
.frame iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.cap{font-size:12px;opacity:.7;margin:10px 2px 0}
.offline{text-align:center;padding:26px 10px}
.offline p{opacity:.8;margin:0 0 16px}
.btn{display:inline-block;background:linear-gradient(90deg,#ff8a3d,#ff3b5c);color:#fff;font-weight:700;
 text-decoration:none;padding:12px 20px;border-radius:999px;box-shadow:0 4px 14px rgba(255,59,92,.35)}
.links{display:flex;flex-wrap:wrap;gap:10px}
.links a{background:rgba(255,244,230,.1);border:1px solid rgba(255,244,230,.22);border-radius:999px;
 padding:8px 14px;font-size:13px;text-decoration:none;color:var(--cream)}
.why p{line-height:1.6;opacity:.92;margin:0 0 12px}
footer{text-align:center;margin-top:30px;font-size:12.5px;opacity:.7}
footer a{color:rgba(255,244,230,.7)}
"""

_WHY = (
    '<p>Gaiverland est né d\'une idée simple, et elle vient de là : et si la fête ne '
    's\'arrêtait jamais&nbsp;? Tomorrowland construit chaque année un monde à part entière — '
    'un lieu, une langue, une ferveur — et le partage avec la planète entière en direct. '
    'C\'est exactement l\'esprit qu\'on essaie de garder allumé toute l\'année.</p>'
    '<p>Leur radio officielle qui tourne 365 jours, leur récit qui se construit d\'une édition '
    'à l\'autre&nbsp;: c\'est notre boussole. Nous, on est petits, permanents et bricolés dans un '
    'camion — mais l\'intention est la même. Rendre à la musique un endroit où elle ne s\'éteint pas.</p>'
    '<p>Alors quand ils sont à l\'antenne, on préfère te renvoyer chez eux plutôt que de faire '
    'semblant. Regarde le vrai, soutiens le vrai. Nous, on reprend juste après.</p>'
)


def render(streams=None) -> str:
    streams = streams or []
    parts = []
    parts.append('<!doctype html><html lang="fr"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append('<title>Gaiverland — Notre modèle : Tomorrowland</title>')
    parts.append('<meta name="robots" content="noindex">')  # page fan/saisonnière, pas d'indexation
    parts.append("<style>" + _CSS + "</style></head><body><div class=\"wrap\">")
    parts.append('<h1 class="logo">Notre modèle</h1>')
    parts.append('<div class="sub">Tomorrowland · Belgique</div>')
    parts.append('<div class="dates">En direct les week-ends du 17–19 et 24–26 juillet 2026</div>')
    parts.append(
        '<div class="disclaim">⚠️ <b>Gaiverland n\'est pas affilié à Tomorrowland</b> et n\'est '
        'pas un partenaire. Tout ce qui suit est diffusé par Tomorrowland via leurs canaux '
        '<b>officiels</b> — nous ne faisons que pointer vers eux. Aucun contenu n\'est ré-hébergé '
        'chez nous&nbsp;: le direct est servi par YouTube, avec leurs pubs et à leur bénéfice. '
        'Tomorrowland® et leurs marques appartiennent à leurs détenteurs.</div>'
    )
    parts.append('<div class="card"><h2>🎪 Les directs officiels</h2>' + _live_block(streams) + '</div>')
    parts.append(
        '<div class="card"><h2>🔗 Chez eux, en officiel</h2><div class="links">'
        '<a href="' + OFFICIAL_LIVE + '" target="_blank" rel="noopener noreferrer">Live YouTube ▶</a>'
        '<a href="' + OFFICIAL_CHANNEL + '" target="_blank" rel="noopener noreferrer">Chaîne officielle</a>'
        '<a href="' + OFFICIAL_SITE + '" target="_blank" rel="noopener noreferrer">tomorrowland.com</a>'
        '</div></div>'
    )
    parts.append('<div class="card why"><h2>💫 Pourquoi c\'est notre modèle</h2>' + _WHY + '</div>')
    parts.append('<footer><a href="/">← Retour au festival permanent</a></footer>')
    parts.append("</div></body></html>")
    return "".join(parts)
