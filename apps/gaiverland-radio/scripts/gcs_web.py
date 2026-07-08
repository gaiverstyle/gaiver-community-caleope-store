"""
GCS Web — Site public Gaiverland (bible v1.1 site_layers).
Une page : Mainstage live + player, scènes secondaires (coming soon),
journal de lore, vote ENCORE/REVIEW/SKIP, ville du festival, galerie (soon).
Design festival (affiche sunset), pas dashboard technique.
Port 8099 — derrière NPM plus tard, accessible en LAN d'ici là.
"""
import os, sys, subprocess

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2, httpx

import json, time
from urllib.parse import urlsplit
import psycopg2.extras
from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse

DB_URL      = os.environ["DATABASE_URL"]
TRACK_URL   = os.environ.get("GCS_TRACK_URL",        "http://gcs-track-service:8090")
STATE_URL   = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")
VOTE_URL    = os.environ.get("GCS_VOTE_URL",         "http://gcs-vote-service:8095")
AZ_URL      = os.environ.get("AZURACAST_URL",        "http://azuracast:80")
AZ_STATION  = int(os.environ.get("AZURACAST_STATION_ID", "1"))
STREAM_URL  = os.environ.get("GCS_STREAM_URL", "")  # override manuel si besoin
# Base publique d'AzuraCast pour réécrire les URLs internes (stream, pochettes)
# que l'API nowplaying renvoie en http://azuracast. Mettre le domaine NPM ici
# quand il existe ; sinon l'IP LAN. Vide = pas de réécriture.
AZ_PUBLIC   = os.environ.get("GCS_AZ_PUBLIC_URL", "").rstrip("/")

app = FastAPI(title="Gaiverland Web")


def _publicize(url: str) -> str:
    """Réécrit une URL AzuraCast (interne docker OU IP LAN) vers la base publique AZ_PUBLIC.
    Appliqué uniquement aux URLs AzuraCast (stream, pochettes) : on remplace le
    scheme://host[:port] par AZ_PUBLIC et on garde le chemin. Sans quoi un visiteur web
    reçoit une URL LAN (http://172.x…) injoignable → pas de son ni de pochette."""
    if not url or not AZ_PUBLIC:
        return url
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url  # déjà relative → laissée telle quelle
    return AZ_PUBLIC + url[len(parts.scheme) + 3 + len(parts.netloc):]

WEATHER_POETRY = {
    "calm":  "Ciel tranquille au-dessus du site",
    "warm":  "L'air est chaud, les basses aussi",
    "windy": "Le vent porte le son plus loin ce soir",
    "storm": "L'orage gronde, le festival répond",
    "rain":  "La pluie danse avec la foule",
    "cold":  "Nuit fraîche, son bouillant",
}

STAGE_LABEL = {
    "mainstage": "Mainstage", "rush": "Rush Stage",
    "sunset": "Sunset Stage", "night": "Night Stage",
}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


@app.get("/health")
def health():
    return {"status": "ok", "service": "gcs-web"}


@app.get("/api/live")
def live():
    out = {"track": {}, "state": {}, "stream_url": STREAM_URL, "art": "", "listeners": 0}
    # AzuraCast nowplaying (art, listen_url, listeners)
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=4)
        if r.status_code == 200:
            np = r.json()
            song = np.get("now_playing", {}).get("song", {})
            out["track"] = {
                "title":    song.get("title", ""),
                "artist":   song.get("artist", ""),
                "elapsed":  np.get("now_playing", {}).get("elapsed", 0),
                "duration": np.get("now_playing", {}).get("duration", 0),
                "song_id":  song.get("id", ""),
            }
            out["art"]       = _publicize(song.get("art", ""))
            out["listeners"] = np.get("listeners", {}).get("current", 0)
            if not out["stream_url"]:
                mounts = np.get("station", {}).get("mounts", [])
                if mounts:
                    out["stream_url"] = mounts[0].get("url", "")
                else:
                    out["stream_url"] = np.get("station", {}).get("listen_url", "")
            out["stream_url"] = _publicize(out["stream_url"])
    except Exception:
        pass
    # Festival state
    try:
        r = httpx.get(f"{STATE_URL}/state/current", timeout=3)
        if r.status_code == 200:
            s = r.json()
            out["state"] = {
                "city":         s.get("city", "Toulon"),
                "stage":        STAGE_LABEL.get(s.get("stage_active", "mainstage"), "Mainstage"),
                "energy":       s.get("energy_level", 3),
                "tod":          s.get("time_of_day", "day"),
                "weather":      WEATHER_POETRY.get(s.get("weather_mood", "calm"),
                                                   WEATHER_POETRY["calm"]),
                "phase":        s.get("festival_phase", "live"),
            }
    except Exception:
        pass
    return out


@app.get("/api/events")
def events(limit: int = 12):
    conn = get_conn()
    with conn.cursor() as cur:
        # Le journal raconte l'HISTOIRE du festival : on exclut les répliques de Rebexis
        # (ce n'est pas le log micro de l'animatrice, c'est le lore du festival).
        cur.execute("""
            SELECT type, description, city, created_at FROM lore_events
            WHERE type <> 'rebexis_intervention'
            ORDER BY created_at DESC LIMIT %s
        """, (min(limit, 30),))
        rows = cur.fetchall()
    conn.close()
    # Ordre chronologique (journal/diary), pas newest-first (log) : on renverse.
    return {"events": [
        {"type": r["type"], "text": r["description"], "city": r["city"],
         "at": r["created_at"].strftime("%H:%M")} for r in reversed(rows)
    ]}


_visuals_cache = {"city": "", "at": 0.0, "imgs": []}


def _city_photos(city: str):
    """Photos de la ville du festival — via Wikipedia REST media-list (fr), caché 30 min, fail-safe.
    On filtre sur les JPG (écarte cartes/blasons/SVG) et on prend la plus grande vignette dispo."""
    if not city:
        return []
    now = time.time()
    if _visuals_cache["city"] == city and now - _visuals_cache["at"] < 1800:
        return _visuals_cache["imgs"]
    imgs = []
    try:
        r = httpx.get(f"https://fr.wikipedia.org/api/rest_v1/page/media-list/{city}",
                      headers={"User-Agent": "GaiverlandRadio/1.0 (festival visuals)"},
                      timeout=6)
        if r.status_code == 200:
            for m in r.json().get("items", []):
                if m.get("type") != "image":
                    continue
                srcset = m.get("srcset") or []
                if not srcset:
                    continue
                src = srcset[-1].get("src", "")  # la plus grande vignette dispo
                if src.startswith("//"):
                    src = "https:" + src
                if src.lower().endswith((".jpg", ".jpeg")):
                    imgs.append(src)
                if len(imgs) >= 15:
                    break
    except Exception:
        pass
    _visuals_cache.update(city=city, at=now, imgs=imgs)
    return imgs


@app.get("/api/visuals")
def visuals():
    """Images du 'clip' in-page : cover courante + photos de la ville. Fail-safe (cover au minimum)."""
    imgs = []
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=4)
        if r.status_code == 200:
            art = _publicize(r.json().get("now_playing", {}).get("song", {}).get("art", ""))
            if art:
                imgs.append(art)
    except Exception:
        pass
    city = ""
    try:
        r = httpx.get(f"{STATE_URL}/state/current", timeout=3)
        if r.status_code == 200:
            city = r.json().get("city", "") or ""
    except Exception:
        pass
    imgs += _city_photos(city)
    return {"images": imgs}


@app.post("/api/vote")
def vote(body: dict = Body(...)):
    v = str(body.get("vote", "")).upper()
    if v not in ("ENCORE", "REVIEW", "SKIP"):
        return {"ok": False, "error": "vote invalide"}
    # Résoudre le morceau en cours
    song_id = ""
    try:
        r = httpx.get(f"{TRACK_URL}/track/current", timeout=3)
        if r.status_code == 200:
            song_id = r.json().get("song_id", "")
    except Exception:
        pass
    if not song_id:
        return {"ok": False, "error": "pas de morceau en cours"}
    try:
        r = httpx.post(f"{VOTE_URL}/vote",
                       json={"song_id": song_id, "vote": v, "user_role": "user"},
                       timeout=5)
        if r.status_code == 200:
            return {"ok": True, "vote": v}
        return {"ok": False, "error": f"vote-service {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


PAGE = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gaiverland — Le festival permanent</title>
<style>
:root{
  --sun1:#ff9a5a; --sun2:#ff5e7a; --sun3:#8b5cf6; --nightblue:#191036;
  --cream:#fff4e6; --ink:#2a1a33;
}
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:Georgia,'Times New Roman',serif;
  background:linear-gradient(175deg,var(--nightblue) 0%,#3d1d5c 30%,var(--sun3) 55%,var(--sun2) 78%,var(--sun1) 100%);
  background-attachment:fixed; color:var(--cream); min-height:100vh;
}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 80px}
header{text-align:center;padding:48px 0 20px}
header .fete{font-size:15px;letter-spacing:6px;text-transform:uppercase;opacity:.85}
header h1{
  font-size:clamp(52px,10vw,96px);letter-spacing:2px;line-height:.95;
  background:linear-gradient(90deg,#ffd29a,#ff8fa3,#c9b6ff);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  text-shadow:0 0 60px rgba(255,150,120,.25);
}
header .tagline{font-style:italic;font-size:18px;margin-top:10px;opacity:.9}
.pennant{display:flex;justify-content:center;gap:6px;margin:18px 0 0;font-size:22px;letter-spacing:8px}
.card{
  background:rgba(255,244,230,.08);border:1px solid rgba(255,244,230,.22);
  border-radius:18px;padding:22px;margin-top:26px;backdrop-filter:blur(8px);
}
h2{font-size:14px;letter-spacing:4px;text-transform:uppercase;opacity:.8;margin-bottom:16px}
.live-badge{display:inline-block;background:#ff3b5c;color:#fff;font-family:sans-serif;
  font-size:11px;font-weight:700;letter-spacing:2px;padding:3px 10px;border-radius:20px;
  animation:pulse 2s infinite;vertical-align:middle;margin-left:10px}
@keyframes pulse{50%{opacity:.55}}
.np{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.np img{width:110px;height:110px;border-radius:14px;object-fit:cover;
  box-shadow:0 8px 30px rgba(0,0,0,.4);background:rgba(0,0,0,.3)}
.np .t{font-size:26px;font-weight:bold}
.np .a{font-size:17px;opacity:.85;font-style:italic;margin-top:4px}
.np .meta{font-size:13px;opacity:.7;margin-top:10px;font-family:sans-serif}
audio{width:100%;margin-top:18px;border-radius:30px}
.bar{height:5px;background:rgba(255,244,230,.18);border-radius:3px;margin-top:14px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ffd29a,#ff8fa3);transition:width 1s linear}
.votes{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}
.votes button{
  flex:1;min-width:110px;padding:14px 8px;border:none;border-radius:14px;cursor:pointer;
  font-family:Georgia,serif;font-size:16px;color:var(--ink);transition:transform .15s;
}
.votes button:hover{transform:translateY(-3px) rotate(-1deg)}
.v-encore{background:linear-gradient(135deg,#ffd29a,#ffb56b)}
.v-review{background:linear-gradient(135deg,#c9b6ff,#a48fff)}
.v-skip{background:linear-gradient(135deg,#ffb1c0,#ff8fa3)}
.votemsg{font-size:14px;margin-top:10px;font-style:italic;min-height:18px;opacity:.9}
.stages{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px}
.stage{border-radius:14px;padding:18px 14px;text-align:center;position:relative;
  border:1px dashed rgba(255,244,230,.35)}
.stage.on{border:1px solid rgba(255,244,230,.5);background:rgba(255,244,230,.1)}
.stage .ico{font-size:30px}
.stage .nm{margin-top:8px;font-size:16px}
.stage .st{font-family:sans-serif;font-size:10px;letter-spacing:2px;text-transform:uppercase;
  margin-top:8px;padding:2px 8px;border-radius:10px;display:inline-block;
  background:rgba(0,0,0,.28);opacity:.9}
.stage.on .st{background:#ff3b5c}
.city{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.city .pin{font-size:44px}
.city .cn{font-size:30px;font-weight:bold}
.city .wx{font-style:italic;opacity:.85;margin-top:4px}
.city .next{margin-left:auto;text-align:right;font-size:14px;opacity:.75;font-style:italic}
.journal .ev{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid rgba(255,244,230,.12);
  font-size:15px;align-items:baseline}
.journal .ev:last-child{border:none}
.journal .at{font-family:sans-serif;font-size:11px;opacity:.6;min-width:42px}
.journal .ic{min-width:24px}
.soon{opacity:.75;text-align:center;padding:26px 10px;font-style:italic;font-size:16px}
footer{text-align:center;margin-top:44px;font-size:13px;opacity:.65;font-style:italic}
footer .c15{margin-top:6px;font-size:12px}
.hero{position:relative;width:100%;aspect-ratio:16/9;border-radius:16px;overflow:hidden;
  background:rgba(0,0,0,.35);box-shadow:0 10px 40px rgba(0,0,0,.45);margin:4px 0 18px}
.hero-layer{position:absolute;inset:0;background-size:cover;background-position:center;
  opacity:0;transition:opacity 1.6s ease;will-change:opacity,transform}
.hero-layer.on{opacity:1;animation:kenburns 14s ease-in-out infinite alternate}
@keyframes kenburns{from{transform:scale(1.03)}to{transform:scale(1.13) translate(-2%,-1.5%)}}
.playbtn{flex:0 0 auto;width:64px;height:64px;border-radius:50%;border:none;cursor:pointer;font-size:24px;color:var(--ink);
  background:linear-gradient(135deg,#ffd29a,#ffb56b);box-shadow:0 6px 24px rgba(0,0,0,.35);transition:transform .15s}
.playbtn:hover{transform:scale(1.06)}
#player{display:none}
</style></head><body><div class="wrap">

<header>
  <div class="fete">✦ le festival permanent ✦</div>
  <h1>GAIVERLAND</h1>
  <div class="tagline">La musique ne s'arrête jamais. Le festival non plus.</div>
  <div class="pennant">🎪 🎡 🎠 🎢 🎆</div>
</header>

<div class="card">
  <h2>Mainstage Broadcast <span class="live-badge">EN DIRECT</span></h2>
  <div class="hero">
    <div class="hero-layer" id="hero-a"></div>
    <div class="hero-layer" id="hero-b"></div>
  </div>
  <div class="np">
    <div style="flex:1;min-width:200px">
      <div class="t" id="title">…</div>
      <div class="a" id="artist"></div>
      <div class="meta" id="meta"></div>
      <div class="bar"><i id="prog"></i></div>
    </div>
    <button id="playbtn" class="playbtn" onclick="togglePlay()" aria-label="Lecture">▶</button>
  </div>
  <audio id="player" preload="none"></audio>
  <div class="votes">
    <button class="v-encore" onclick="vote('ENCORE')">🔥 ENCORE</button>
    <button class="v-review" onclick="vote('REVIEW')">🤔 À REVOIR</button>
    <button class="v-skip"   onclick="vote('SKIP')">⏭ PASSER</button>
  </div>
  <div class="votemsg" id="votemsg"></div>
</div>

<div class="card">
  <h2>La tournée</h2>
  <div class="city">
    <div class="pin">📍</div>
    <div>
      <div class="cn" id="city">…</div>
      <div class="wx" id="wx"></div>
    </div>
    <div class="next">prochaine ville :<br>le convoi décidera. 🚐</div>
  </div>
</div>

<div class="card">
  <h2>Les scènes</h2>
  <div class="stages">
    <div class="stage on"><div class="ico">🎪</div><div class="nm">Mainstage</div><div class="st">Live</div></div>
    <div class="stage"><div class="ico">⚡</div><div class="nm">Rush Stage</div><div class="st">Bientôt</div></div>
    <div class="stage"><div class="ico">🌅</div><div class="nm">Sunset Stage</div><div class="st">Bientôt</div></div>
    <div class="stage"><div class="ico">🌙</div><div class="nm">Night Stage</div><div class="st">Bientôt</div></div>
    <div class="stage"><div class="ico">💫</div><div class="nm">Pulse Stage</div><div class="st">Bientôt</div></div>
  </div>
</div>

<div class="card journal">
  <h2>Journal du festival</h2>
  <div id="events"><div class="soon">Le journal s'écrit en ce moment même…</div></div>
</div>

<div class="card">
  <h2>Galerie</h2>
  <div class="soon">📸 Les souvenirs du festival arrivent bientôt.<br>
  Le stagiaire a promis de retrouver la carte SD.</div>
</div>

<footer>
  Gaiverland Radio — présente, comme toujours.
  <div class="c15">Le C15 veille sur ce site. Personne ne sait pourquoi.</div>
</footer>

</div><script>
const ICO={rebexis_intervention:'🎙',c15_event:'🚐',stagiaire_event:'🧢',city_transition:'📍'};
let audioUrl="";
let visuals=[], vi=0, heroToggle=false;

function togglePlay(){
  const a=document.getElementById('player');
  if(audioUrl && !a.src) a.src=audioUrl;
  if(a.paused){ a.play().catch(()=>{}); } else { a.pause(); }
}
// Clip in-page : slideshow (cover de l'artiste + photos de la ville) — image qui change souvent.
async function loadVisuals(){
  try{
    const d=await (await fetch('/api/visuals')).json();
    if(d.images && d.images.length) visuals=d.images;
  }catch(e){}
}
function nextVisual(){
  if(!visuals.length) return;
  const url=visuals[vi % visuals.length]; vi++;
  const img=new Image();
  img.onload=()=>{
    const a=document.getElementById('hero-a'), b=document.getElementById('hero-b');
    const show=heroToggle?b:a, hide=heroToggle?a:b;
    show.style.backgroundImage="url(\""+url.replace(/"/g,'%22')+"\")";
    show.classList.add('on'); hide.classList.remove('on');
    heroToggle=!heroToggle;
  };
  img.onerror=()=>{};  // image cassée → on garde l'actuelle, l'image suivante passera au prochain tick
  img.src=url;
}
async function refresh(){
  try{
    const d=await (await fetch('/api/live')).json();
    const t=d.track||{};
    document.getElementById('title').textContent=t.title||'Gaiverland Radio';
    document.getElementById('artist').textContent=t.artist||'';
    const l=d.listeners?d.listeners+' personne(s) dans la foule':'';
    document.getElementById('meta').textContent=l;
    // Media Session — titre/artiste/cover sur l'écran verrouillé + widgets média de l'OS
    if('mediaSession' in navigator && (t.title||t.artist)){
      navigator.mediaSession.metadata=new MediaMetadata({
        title:t.title||'Gaiverland Radio', artist:t.artist||'Gaiverland Radio',
        album:'Gaiverland — le festival permanent',
        artwork:d.art?[{src:d.art,sizes:'512x512',type:'image/jpeg'}]:[]
      });
    }
    if(t.duration>0){document.getElementById('prog').style.width=Math.min(100,100*t.elapsed/t.duration)+'%';}
    if(d.stream_url){audioUrl=d.stream_url;}
    const s=d.state||{};
    document.getElementById('city').textContent=s.city||'Quelque part';
    document.getElementById('wx').textContent=(s.weather||'')+(s.stage?' — scène active : '+s.stage:'');
  }catch(e){}
}
async function loadEvents(){
  try{
    const d=await (await fetch('/api/events')).json();
    if(!d.events||!d.events.length)return;
    document.getElementById('events').innerHTML=d.events.map(e=>
      '<div class="ev"><span class="at">'+e.at+'</span><span class="ic">'+(ICO[e.type]||'✦')+
      '</span><span>'+e.text.replace(/</g,'&lt;')+'</span></div>').join('');
  }catch(e){}
}
async function vote(v){
  const m=document.getElementById('votemsg');
  try{
    const r=await (await fetch('/api/vote',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({vote:v})})).json();
    m.textContent=r.ok?'Vote "'+v+'" enregistré. Le festival vous a entendu. ✦'
                       :'Hmm… '+(r.error||'réessayez');
  }catch(e){m.textContent='Le stagiaire a débranché quelque chose. Réessayez.';}
  setTimeout(()=>m.textContent='',6000);
}
(function(){const a=document.getElementById('player'),b=document.getElementById('playbtn');
 a.addEventListener('play',()=>{b.textContent='⏸'; if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing';});
 a.addEventListener('pause',()=>{b.textContent='▶'; if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused';});
 if('mediaSession' in navigator){
   navigator.mediaSession.setActionHandler('play',togglePlay);
   navigator.mediaSession.setActionHandler('pause',()=>document.getElementById('player').pause());
 }})();
refresh();loadEvents();
loadVisuals().then(()=>{ nextVisual(); setInterval(nextVisual,7000); });
setInterval(refresh,10000);setInterval(loadEvents,30000);setInterval(loadVisuals,45000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8099)
