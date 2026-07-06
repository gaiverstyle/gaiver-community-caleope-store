# Gaiverland — Tech Architecture v1

> Compatibilité : penser **Caleope store** (services = apps Caleope, compose Caleope, params Caleope).

**Dépend de :** [Gaiverland Bible v1.1](GAIVERLAND_BIBLE.md) · [Pack Claude v1](GAIVERLAND_PACK_v1.md)

---

## 1. Vue d'ensemble

Objectif : système modulaire qui lit un flux AzuraCast, analyse les morceaux, met à jour l'état du festival, génère des interventions Rebexis, injecte l'audio dans le stream, gère votes + mémoire lore.

```
[AzuraCast Stream]
        ↓
[Track Listener Service]
        ↓
[State Engine — GSE]
        ↓
[Rebexis Engine — RPE]
        ↓
[ElevenLabs TTS Service]
        ↓
[Audio Injector]
        ↓
[Final Stream Output]
```

---

## 2. Services (containers)

| Service                | Container Caleope actuel | Statut       |
|------------------------|--------------------------|--------------|
| gaiver-state-engine    | gw-playlist + gw-scheduler | Partiel    |
| rebexis-engine         | gw-rebexis               | Implémenté   |
| tts-service            | gw-tts                   | Implémenté   |
| audio-injector         | Liquidsoap/AzuraCast     | Partiel      |
| vote-service           | —                        | À créer      |
| lore-service           | — (rebexis_sessions DB)  | À créer      |

### gaiver-state-engine (GSE)
- track analysis → state update, mood calculation
- `POST /state/update` · `GET /state/current`

### rebexis-engine (RPE)
- génère scripts Rebexis, applique templates, produit JSON final
- `POST /generate`

### tts-service
- texte → audio, cache `hash(text + emotion + voice_id)`, rate limit
- `POST /tts` · `GET /cache/{id}`

### audio-injector
- mix TTS + stream AzuraCast, ducking musique si voice active

### vote-service
- ENCORE / REVIEW / SKIP → weighted score → influence state
- weights : founder 0.6 · users 0.3 · system_ai 0.1

### lore-service
- mémoire : villes, events C15, stagiaire, historique Rebexis
- event type : `{ "type": "c15_event", "description": "...", "city": "...", "timestamp": "auto" }`

---

## 3. Loop principal (temps réel)

```
1. AzuraCast joue track
2. Track Listener détecte metadata
3. GSE update state
4. Vote system update (optionnel)
5. RPE génère intervention
6. TTS génère audio (cache first)
7. Audio Injector injecte dans stream
8. Lore Service log event
```

---

## 4. State Engine

```json
{
  "city": "Toulon",
  "stage": "mainstage",
  "festival_phase": "live",
  "energy_level": 3,
  "time_of_day": "sunset",
  "weather_mood": "calm"
}
```

Énergie : +1 track haute · -1 track calme · clamp 1–5

Stage mapping : drift→sunset · pulse→mainstage · festival→mainstage · rush→rush · night→night

---

## 5. TTS Budget

```yaml
monthly_budget:
  max_tokens: 10000
  reserved:
    system: 60%
    experimental: 20%
    personal: 20%
```

---

## 6. Règles

- 1 service = 1 container
- communication via REST (ou event bus)
- CPU only, pas de GPU
- **No service is allowed to directly generate narrative outside of RPE**

## 7. Priorité système

1. Audio continuity
2. Festival immersion
3. Stability
4. Performance
