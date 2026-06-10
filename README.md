# Gaiver Community — Caleope Store

Packages Caleope personnels pour l'écosystème [Caleope](https://github.com/Gaiver-IT/caleope-store).

## Packages disponibles

| Package | Description |
|---------|-------------|
| [`gaiverland-radio`](apps/gaiverland-radio/) | Radio autonome avec IA : AzuraCast + animatrice Rebexis + moteur de playlist intelligent |

## Installation

Dans Caleope, pointez vers ce store community :

```bash
# Ajouter le store community
caleope store add https://github.com/gaiverstyle/gaiver-community-caleope-store

# Installer un package
caleope install gaiverland-radio
```

## Structure

```
apps/
└── gaiverland-radio/
    ├── app.json          # Définition du package
    ├── params.json       # Paramètres configurables
    ├── docker-compose.yml
    ├── setup.sh          # Script d'installation
    └── REQUIREMENTS.md
```
