# Gaiverland — MVP Implementation Plan v1

> Compatibilité : penser **Caleope store** — mapper chaque service sur les containers existants avant de créer du nouveau.

**Dépend de :** [Bible v1.1](GAIVERLAND_BIBLE.md) · [Pack v1](GAIVERLAND_PACK_v1.md) · [Tech Arch v1](GAIVERLAND_TECH_ARCH_v1.md)

---

## Objectif

1 radio principale · 1 pipeline AzuraCast · 1 Rebexis TTS · 1 state engine simple · 1 injection audio

## Hors MVP (interdit au départ)

Autres radios · site web complet · lore avancé · multi-villes · vote complexe · IA musicale avancée

---

## Phase 1 — Track Listener

Input : AzuraCast Now Playing API  
Output : `{ "title": "", "artist": "", "genre": "", "timestamp": "" }`

## Phase 2 — State Engine

```json
{ "energy_level": 3, "last_genre": "none", "last_artist": "none" }
```
Règles : genre énergique → +1 · genre calme → -1 · clamp 1–5

## Phase 3 — Rebexis Engine

Input : `{ "track": {}, "state": {} }`  
Output : `{ "text": "string", "emotion": "playful|excited|calm|amused|energetic" }`

**PAS de génération libre — uniquement templates.**

Templates minimum (6) :

| Contexte       | Template                                                         |
|----------------|------------------------------------------------------------------|
| INTRO          | `Bon… {artist} arrive sur le festival.`                         |
| HIGH ENERGY    | `Ok là ça monte clairement. On y va avec {title}.`              |
| SIMPLE ANNOUNCE| `On écoute {title}.`                                            |
| HUMOUR         | `Petit check backstage… le stagiaire est encore introuvable.`   |
| TRANSITION     | `On ne ralentit pas. On continue.`                              |
| CALM           | `On reste dans l'ambiance du festival.`                         |

## Phase 4 — TTS

`POST /tts { "text": "", "emotion": "" }`  
Cache key : `hash(text + emotion)`

## Phase 5 — Audio Injection

1. play music → 2. TTS ready → 3. duck music → 4. play voice → 5. restore music

## Phase 6 — Services

```
- azuracast (existant)
- track-listener
- state-engine
- rebexis-engine
- tts-service
- audio-injector
```

1 service = 1 container · REST API only · no shared state except DB

## Phase 7 — DB minimale

`tracks_log` : id, title, artist, timestamp  
`state` : energy_level, last_update

## Phase 8 — Règles absolues

- no new characters · no new systems · no lore expansion · no AI creativity outside templates

## Phase 9 — Acceptance test

MVP validé si : musique joue · Rebexis parle entre tracks · énergie évolue · audio injecté proprement · stable 1h+ sans crash

## Ordre de build

```
STEP 1 → Track Listener
STEP 2 → State Engine
STEP 3 → Rebexis Engine (templates)
STEP 4 → TTS service
STEP 5 → Audio injector
STEP 6 → Integration test full loop
```
