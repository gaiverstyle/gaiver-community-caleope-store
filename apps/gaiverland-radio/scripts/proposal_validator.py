import os, re, json, urllib.request, urllib.parse, psycopg2, psycopg2.extras as E
ACCEPT_GENRES={'dance','electronic','house','techno','trance','dubstep','drum & bass','drum and bass',
  'edm','electro','dance/electronic','breakbeat','garage','ambient','downtempo','nu disco','disco',
  'hardstyle','hardcore','hard dance','future bass','bass','trap','electronica'}
# Empreinte musique IA / stock / library de fond → rejet MÊME si le genre est électro
# (une pochette IA « Progressive House Anthem » se fait taguer House et passait le filtre).
# ⚠️ RÈGLE CHEF (11/07) : NE PAS rejeter sur « no copyright »/« NCS » ni « free download » —
# NoCopyrightSounds = classiques electro d'internet (Cartoon, Tobu, Elektronomia…) et plein
# d'edits/bootlegs de DJ légitimes sont en free download. Le marqueur de déchet = l'empreinte
# library/stock/IA (titres descriptifs génériques, chaînes de stock, gabarits), PAS le libre de droits.
STOCK_RE=re.compile(
  r'background music|royalty[ -]?free|ableton template|progressive house anthem'
  r'|emotional (edm|progressive) anthem|driving techno|stream the vibes|click buy'
  r'|uplifting (background|corporate)|no\.?\s*copyright\s*(background|music library)'
  r'|\b(ashamaluevmusic|aivora|ceamusic|odaystar|cyber beat lab|infinite music vault'
  r'|we make dance music|aky anife|q-?bale|lyvn fade|encourage recordings)\b', re.I)
def looks_stock(*parts):
    return bool(STOCK_RE.search(" ".join(p for p in parts if p)))
def _get(url):
    return json.load(urllib.request.urlopen(url, timeout=10))
def genre_lookup(q):
    # 1. iTunes : genre direct (primaryGenreName)
    try:
        d=_get("https://itunes.apple.com/search?"+urllib.parse.urlencode({"term":q,"entity":"song","limit":1}))
        if d.get("results"):
            r=d["results"][0]; return r.get("primaryGenreName"), r["artistName"], r["trackName"]
    except Exception: pass
    # 2. Deezer fallback : search -> album -> genres
    try:
        d=_get("https://api.deezer.com/search?"+urllib.parse.urlencode({"q":q,"limit":1}))
        if d.get("data"):
            t=d["data"][0]; alb=_get(f"https://api.deezer.com/album/{t['album']['id']}")
            gs=[g['name'] for g in alb.get('genres',{}).get('data',[])]
            return (gs[0] if gs else None), t['artist']['name'], t['title']
    except Exception: pass
    return None, "", ""
def main():
    c=psycopg2.connect(os.environ['DATABASE_URL'], cursor_factory=E.RealDictCursor); cur=c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS proposal_decisions (
        title TEXT PRIMARY KEY, verdict TEXT, genre TEXT, artist TEXT,
        canon_title TEXT, votes INT, decided_at TIMESTAMP DEFAULT NOW())""")
    # title_proposals est alimentée par gcs_web ; on la garantit ici pour pouvoir
    # tourner même avant la première proposition (sinon SELECT sur table absente).
    cur.execute("""CREATE TABLE IF NOT EXISTS title_proposals (
        id SERIAL PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
        title TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
    c.commit()
    cur.execute("SELECT title, count(DISTINCT user_id) votes FROM title_proposals GROUP BY title ORDER BY votes DESC")
    props=cur.fetchall(); accepted=[]; na=nr=skip=0
    for p in props:
        cur.execute("SELECT 1 FROM proposal_decisions WHERE title=%s",(p['title'],))
        if cur.fetchone(): skip+=1; continue
        g,artist,canon=genre_lookup(p['title'])
        if looks_stock(p['title'], artist, canon):
            verdict='reject'; reason='IA/stock'
        elif g and g.lower() in ACCEPT_GENRES:
            verdict='accept'; reason=g
        else:
            verdict='reject'; reason=g or 'genre inconnu'
        cur.execute("""INSERT INTO proposal_decisions (title,verdict,genre,artist,canon_title,votes)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (title) DO NOTHING""",
                    (p['title'],verdict,g,artist,canon,p['votes']))
        if verdict=='accept': accepted.append(f"{artist} - {canon}"); na+=1
        else: nr+=1
        print(f"{'✅ ACCEPT' if verdict=='accept' else '🚫 reject'} [{reason}] ({p['votes']} votes)  {p['title']}")
    c.commit()
    print(f"=== {na} acceptés, {nr} rejetés, {skip} déjà décidés ===")
    if accepted:
        print("--- À TÉLÉCHARGER ---")
        for a in accepted: print(a)
if __name__=="__main__": main()
