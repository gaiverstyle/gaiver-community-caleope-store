#!/bin/bash
set -euo pipefail

CONFIG_DIR="${CALEOPE_BASE_DIR}/app-config/${CALEOPE_APP_ID}"
SRC_DIR="${CONFIG_DIR}/src"
mkdir -p "${SRC_DIR}"

# ── Vérifications obligatoires ──────────────────────────────────────────────
MISSING=()
[ -z "${CALEOPE_PARAM_DISCORD_TOKEN:-}"      ] && MISSING+=("DISCORD_TOKEN")
[ -z "${CALEOPE_PARAM_AZURACAST_URL:-}"      ] && MISSING+=("AZURACAST_URL")
[ -z "${CALEOPE_PARAM_AZURACAST_STATION_ID:-}" ] && MISSING+=("AZURACAST_STATION_ID")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  ⚠ Paramètres manquants : ${MISSING[*]}"
    echo "  Configure via : caleope configure ${CALEOPE_APP_ID}"
fi

# ── secrets.env ─────────────────────────────────────────────────────────────
# IDEMPOTENT — ne JAMAIS écraser un secret existant par du vide.
# Un `install --force` ne redemande PAS les paramètres (ils ne sont saisis qu'à la 1re
# installation) : les CALEOPE_PARAM_* sont donc vides. L'ancienne version écrivait alors
# `DISCORD_TOKEN=` et détruisait le token + toute la config → bot en crash-loop
# « Improper token has been passed » (incident 14/07, restauré depuis la sauvegarde 04:30).
# Priorité : paramètre fourni > valeur déjà en place > défaut.
SECRETS="${CONFIG_DIR}/secrets.env"

_prev() {   # _prev <CLE> → valeur actuelle dans secrets.env (vide si absente)
    [ -f "${SECRETS}" ] || return 0
    sed -n "s/^$1=//p" "${SECRETS}" | head -1
}
_keep() {   # _keep <CLE> <valeur_param> <defaut>
    local v="$2"
    [ -n "${v}" ] || v="$(_prev "$1")"
    [ -n "${v}" ] || v="$3"
    printf '%s=%s\n' "$1" "${v}"
}

# On génère à côté puis on bascule : _prev() lit l'ancien fichier pendant l'écriture.
{
    _keep DISCORD_TOKEN        "${CALEOPE_PARAM_DISCORD_TOKEN:-}"        ""
    _keep AZURACAST_URL        "${CALEOPE_PARAM_AZURACAST_URL:-}"        ""
    _keep AZURACAST_STATION_ID "${CALEOPE_PARAM_AZURACAST_STATION_ID:-}" "radio"
    _keep AZURACAST_API_KEY    "${CALEOPE_PARAM_AZURACAST_API_KEY:-}"    ""
    _keep STREAM_URL           "${CALEOPE_PARAM_STREAM_URL:-}"           ""
    _keep AUTO_CHANNEL_ID      "${CALEOPE_PARAM_AUTO_CHANNEL_ID:-}"      ""
    _keep DEFAULT_VOLUME       "${CALEOPE_PARAM_DEFAULT_VOLUME:-}"       "100"
    _keep NP_CHANNEL_ID        "${CALEOPE_PARAM_NP_CHANNEL_ID:-}"        ""
    _keep NP_POLL_INTERVAL     "${CALEOPE_PARAM_NP_POLL_INTERVAL:-}"     "10"
    _keep SITE_URL             "${CALEOPE_PARAM_SITE_URL:-}"             ""
} > "${SECRETS}.new"
mv -f "${SECRETS}.new" "${SECRETS}"
chmod 600 "${SECRETS}"

# ── requirements.txt ─────────────────────────────────────────────────────────
cat > "${SRC_DIR}/requirements.txt" << 'PYREQ'
discord.py[voice]>=2.4.0
aiohttp>=3.9.0
PyNaCl>=1.5.0
PYREQ

# ── bot.py ───────────────────────────────────────────────────────────────────
cat > "${SRC_DIR}/bot.py" << 'PYEOF'
#!/usr/bin/env python3
"""AzuraCast Radio Bot — diffuse ta radio en continu dans Discord."""

import asyncio
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("radio-bot")

DISCORD_TOKEN        = os.environ["DISCORD_TOKEN"]
AZURACAST_URL        = os.environ["AZURACAST_URL"].rstrip("/")
AZURACAST_STATION_ID = os.environ.get("AZURACAST_STATION_ID", "radio")

# Liens mp3 publics par station — routes same-origin servies par le site Gaiverland.
# (Le flux interne http://azuracast/... n'est PAS partageable : il n'existe que dans Docker.)
PUBLIC_BASE = os.environ.get("GAIVERLAND_PUBLIC_URL", "https://gaiverland.gaiver-it.fr").rstrip("/")
STATION_MP3 = {
    "gaiverlandradio":      ("Mainstage",  "/live.mp3"),
    "gaiverland_chill":     ("Chill",      "/chill.mp3"),
    "gaiverland_hard":      ("Hard",       "/hard.mp3"),
    "gaiverland_phonk":     ("Phonk",      "/phonk.mp3"),
    "gaiverland_lofi":      ("Lo-fi",      "/lofi.mp3"),
    "gaiverland_synthwave": ("Synthwave",  "/synthwave.mp3"),
}
AZURACAST_API_KEY    = os.environ.get("AZURACAST_API_KEY", "")
STREAM_URL_ENV       = os.environ.get("STREAM_URL", "").strip()
AUTO_CHANNEL_ID      = int(os.environ.get("AUTO_CHANNEL_ID", "0") or "0")
DEFAULT_VOLUME       = max(0, min(200, int(os.environ.get("DEFAULT_VOLUME", "100") or "100")))
NP_CHANNEL_ID        = int(os.environ.get("NP_CHANNEL_ID", "0") or "0")
NP_POLL_INTERVAL     = max(5, int(os.environ.get("NP_POLL_INTERVAL", "10") or "10"))
SITE_URL             = os.environ.get("SITE_URL", "").strip().rstrip("/")   # site des votes auditeurs (optionnel)


# ── Player ───────────────────────────────────────────────────────────────────

class RadioPlayer:
    def __init__(self):
        self.voice_client: discord.VoiceClient | None = None
        self.volume: float = DEFAULT_VOLUME / 100.0
        self.station: str = AZURACAST_STATION_ID          # station courante (défaut = AZURACAST_STATION_ID)
        self._stream_cache: dict = {AZURACAST_STATION_ID: STREAM_URL_ENV} if STREAM_URL_ENV else {}

    # -- AzuraCast API --------------------------------------------------------

    async def _get(self, path: str) -> dict:
        headers = {"X-API-Key": AZURACAST_API_KEY} if AZURACAST_API_KEY else {}
        url = f"{AZURACAST_URL}{path}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    return await r.json()
        except Exception as exc:
            log.warning("AzuraCast GET %s → %s", path, exc)
            return {}

    async def fetch_now_playing(self) -> dict:
        return await self._get(f"/api/nowplaying/{self.station}")

    async def fetch_stations(self) -> list:
        """Liste (shortcode, nom) des stations AzuraCast — alimente /radio station."""
        data = await self._get("/api/stations")
        out = []
        for s in (data if isinstance(data, list) else []):
            sc = s.get("shortcode") or s.get("id")
            if sc:
                out.append((str(sc), str(s.get("name") or sc)))
        return out

    async def fetch_stream_url(self) -> str:
        # Override explicite (STREAM_URL) mis en cache à l'init : prioritaire.
        if self._stream_cache.get(self.station):
            return self._stream_cache[self.station]
        np = await self.fetch_now_playing()
        station = np.get("station", {})
        mounts = station.get("mounts", [])
        # Générique : on utilise l'URL de flux fournie par AzuraCast (pas de port en dur).
        # Chaque instance annonce ses propres mounts/ports via l'API.
        url = ""
        for m in mounts:
            murl = m.get("url", "")
            if murl:
                url = murl
                if m.get("is_default"):
                    break
        if not url:
            url = station.get("listen_url", "") or station.get("hls_url", "")
        if url:
            self._stream_cache[self.station] = url
        return url

    # -- Lecture vocale -------------------------------------------------------

    def _make_source(self, url: str) -> discord.FFmpegOpusAudio:
        return discord.FFmpegOpusAudio(
            url,
            before_options=(
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                "-analyzeduration 0 -loglevel warning"
            ),
            options=f"-vn -filter:a volume={self.volume:.3f}",
        )

    async def play(self, channel: discord.VoiceChannel) -> tuple[bool, str]:
        stream_url = await self.fetch_stream_url()
        if not stream_url:
            return False, "URL du stream introuvable. Vérifie AZURACAST_URL et AZURACAST_STATION_ID."

        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel.id != channel.id:
                await self.voice_client.move_to(channel)
        else:
            self.voice_client = await channel.connect()

        if self.voice_client.is_playing():
            self.voice_client.stop()

        source = self._make_source(stream_url)
        self.voice_client.play(source, after=self._after)
        return True, stream_url

    def _after(self, exc: Exception | None):
        if exc:
            log.error("Erreur lecteur : %s", exc)

    async def stop(self):
        if self.voice_client:
            if self.voice_client.is_playing():
                self.voice_client.stop()
            await self.voice_client.disconnect()
            self.voice_client = None

    async def restart_with_volume(self):
        """Relance le stream pour appliquer le nouveau volume."""
        if not (self.voice_client and self.voice_client.is_connected()):
            return
        channel = self.voice_client.channel
        if self.voice_client.is_playing():
            self.voice_client.stop()
        stream_url = await self.fetch_stream_url()
        if stream_url:
            source = self._make_source(stream_url)
            self.voice_client.play(source, after=self._after)

    async def set_station(self, station: str) -> bool:
        """Change la station courante et relance le flux en vocal si en écoute.

        L'ORDRE COMPTE. L'ancienne version faisait `self.station = station` en PREMIER,
        puis tentait de relancer le flux. Si le bot n'était plus connecté au vocal (ça
        arrive : coupure gateway + reconnexion), tout le bloc de relance était sauté et
        la fonction renvoyait True quand même → le bot ANNONÇAIT la nouvelle station et
        les votes/blacklist s'y appliquaient, alors que le SON restait l'ancien. C'est
        comme ça qu'on blackliste un titre qu'on n'écoute pas (incident 14/07).
        Désormais : on résout le flux d'ABORD, et on ne bascule l'état QUE si le son suit.
        """
        if station == self.station:
            return True

        previous, self.station = self.station, station
        url = await self.fetch_stream_url()          # résout d'après self.station (la nouvelle)
        if not url:
            self.station = previous                  # rien n'a bougé → ne pas mentir sur l'état
            log.warning("Station %s : flux introuvable → on reste sur %s", station, previous)
            return False

        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.is_playing():
                self.voice_client.stop()
                # stop() n'est pas instantané : le thread lecteur doit rendre la main,
                # sinon play() lève ClientException(« Already playing audio »).
                for _ in range(20):
                    if not self.voice_client.is_playing():
                        break
                    await asyncio.sleep(0.05)
            try:
                self.voice_client.play(self._make_source(url), after=self._after)
            except Exception as exc:
                self.station = previous
                log.error("Relance du flux sur %s échouée : %s", station, exc)
                return False
            log.info("Station : %s → %s (%s)", previous, station, url)
        else:
            log.info("Station : %s → %s (hors vocal — effectif au prochain /radio play)",
                     previous, station)
        return True

    @property
    def is_playing(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_playing())


# ── Bot ──────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
player = RadioPlayer()


# ── Helpers embed ─────────────────────────────────────────────────────────────

def np_embed(np: dict) -> discord.Embed:
    embed = discord.Embed(color=0x3498DB)
    if not np:
        embed.title = "❓ Aucune info disponible"
        return embed
    song    = np.get("now_playing", {}).get("song", {})
    station = np.get("station", {})
    elapsed = np.get("now_playing", {}).get("elapsed", 0)
    duration = np.get("now_playing", {}).get("duration", 0)
    listeners = np.get("listeners", {}).get("current", 0)

    title  = song.get("title") or "—"
    artist = song.get("artist") or "—"
    album  = song.get("album") or ""
    art    = song.get("art") or ""

    embed.title = f"🎵 {title}"
    embed.description = f"**{artist}**" + (f" — {album}" if album else "")

    def fmt_time(s: int) -> str:
        return f"{s // 60}:{s % 60:02d}"

    if duration:
        embed.add_field(name="Durée", value=f"{fmt_time(elapsed)} / {fmt_time(duration)}", inline=True)
    embed.add_field(name="Auditeurs", value=str(listeners), inline=True)
    if SITE_URL:
        embed.add_field(name="🗳️ Voter", value=f"[ENCORE / SKIP sur le site]({SITE_URL})", inline=True)
    if art and art.startswith("https://"):
        embed.set_thumbnail(url=art)
    embed.set_footer(text=station.get("name", AZURACAST_STATION_ID))
    return embed


# ── Now Playing Tracker ───────────────────────────────────────────────────────

class NowPlayingTracker:
    """Poste et maintient à jour un message 'En ce moment' dans un salon texte."""

    def __init__(self):
        self.message: discord.Message | None = None
        self._last_song_id: str = ""

    async def _find_existing(self, channel: discord.TextChannel) -> discord.Message | None:
        """Cherche un message existant du bot dans les 50 derniers messages."""
        async for msg in channel.history(limit=50):
            if msg.author == bot.user and msg.embeds:
                footer = msg.embeds[0].footer.text or ""
                if "NowPlaying" in footer:
                    return msg
        return None

    async def start(self, channel: discord.TextChannel, np: dict):
        embed = self._build_embed(np)
        existing = await self._find_existing(channel)
        if existing:
            await existing.edit(embed=embed)
            self.message = existing
            log.info("Message NP existant récupéré dans #%s", channel.name)
        else:
            self.message = await channel.send(embed=embed)
            log.info("Message NP créé dans #%s", channel.name)
        song = np.get("now_playing", {}).get("song", {})
        self._last_song_id = song.get("id", "") or song.get("title", "")

    async def update(self, np: dict):
        if not self.message:
            return
        song = np.get("now_playing", {}).get("song", {})
        song_id = song.get("id", "") or song.get("title", "")
        if song_id == self._last_song_id:
            return  # Pas de changement
        self._last_song_id = song_id
        try:
            embed = self._build_embed(np)
            await self.message.edit(embed=embed)
            log.info("NP mis à jour : %s — %s", song.get("artist", "?"), song.get("title", "?"))
        except discord.NotFound:
            self.message = None  # Message supprimé, on le recréera au prochain cycle
        except Exception as exc:
            log.warning("Impossible d'éditer le message NP : %s", exc)

    def _build_embed(self, np: dict) -> discord.Embed:
        station = np.get("station", {})
        song    = np.get("now_playing", {}).get("song", {})
        nxt     = np.get("playing_next", {}).get("song", {})
        listeners = np.get("listeners", {}).get("current", 0)
        elapsed   = np.get("now_playing", {}).get("elapsed", 0)
        duration  = np.get("now_playing", {}).get("duration", 0)

        title  = song.get("title") or "—"
        artist = song.get("artist") or "—"
        album  = song.get("album") or ""
        art    = song.get("art") or ""

        embed = discord.Embed(
            title=f"🎵 {title}",
            description=f"**{artist}**" + (f"\n_{album}_" if album else ""),
            color=0x1DB954,
        )

        if duration:
            def t(s):
                return f"{s // 60}:{s % 60:02d}"
            bar_len = 16
            filled = int(bar_len * elapsed / duration) if duration else 0
            bar = "▰" * filled + "▱" * (bar_len - filled)
            embed.add_field(name="Progression", value=f"`{t(elapsed)}` {bar} `{t(duration)}`", inline=False)

        embed.add_field(name="👥 Auditeurs", value=str(listeners), inline=True)
        embed.add_field(name="📻 Station",   value=station.get("name", AZURACAST_STATION_ID), inline=True)

        if nxt:
            nxt_title  = nxt.get("title", "—")
            nxt_artist = nxt.get("artist", "")
            embed.add_field(
                name="⏭️ Ensuite",
                value=f"{nxt_artist} — {nxt_title}" if nxt_artist else nxt_title,
                inline=False,
            )

        if art and art.startswith("https://"):
            embed.set_thumbnail(url=art)

        embed.set_footer(text=f"NowPlaying • {AZURACAST_URL}")
        return embed


np_tracker = NowPlayingTracker()


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Connecté : %s (id %s)", bot.user, bot.user.id)
    # Sync PAR SERVEUR (instantané) et pas seulement en global.
    # `tree.sync()` global : Discord met jusqu'à 1 H à propager → le client affiche
    # « Cette commande est obsolète » et l'autocomplétion d'une commande fraîchement
    # ajoutée ne remonte pas. Les commandes de guilde, elles, sont actives tout de suite
    # et MASQUENT les globales de même nom (pas de doublon).
    for g in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
            log.info("Commandes synchronisées sur « %s » (instantané)", g.name)
        except Exception as exc:
            log.warning("Sync guilde %s échouée : %s", g.name, exc)
    await bot.tree.sync()          # global aussi : utile si le bot rejoint un autre serveur
    log.info("Slash commands synchronisées")

    if AUTO_CHANNEL_ID:
        channel = bot.get_channel(AUTO_CHANNEL_ID)
        if isinstance(channel, discord.VoiceChannel):
            ok, info = await player.play(channel)
            if ok:
                log.info("Auto-join #%s → lecture lancée", channel.name)
            else:
                log.warning("Auto-join échoué : %s", info)

    if NP_CHANNEL_ID:
        channel = bot.get_channel(NP_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            np = await player.fetch_now_playing()
            if np:
                await np_tracker.start(channel, np)
        poll_now_playing.start()

    update_presence.start()


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Repart si le bot est seul dans le salon (tous les humains sont partis)."""
    if not player.voice_client:
        return
    bot_channel = player.voice_client.channel
    if len([m for m in bot_channel.members if not m.bot]) == 0:
        log.info("Salon vide — pause")
        if player.voice_client.is_playing():
            player.voice_client.pause()


@tasks.loop(minutes=1)
async def update_presence():
    np = await player.fetch_now_playing()
    song = np.get("now_playing", {}).get("song", {}) if np else {}
    title  = song.get("title", "")
    artist = song.get("artist", "")
    text = f"{artist} — {title}" if artist and title else (title or artist or "AzuraCast Radio")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name=text[:128])
    )


@tasks.loop(seconds=NP_POLL_INTERVAL)
async def poll_now_playing():
    """Vérifie le titre en cours et édite le message si ça a changé."""
    if not np_tracker.message:
        # Message perdu (supprimé) → on le recrée
        channel = bot.get_channel(NP_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            np = await player.fetch_now_playing()
            if np:
                await np_tracker.start(channel, np)
        return
    np = await player.fetch_now_playing()
    if np:
        await np_tracker.update(np)


# ── Slash commands ────────────────────────────────────────────────────────────

radio_group = app_commands.Group(name="radio", description="Commandes du bot radio AzuraCast")


@radio_group.command(name="play", description="Rejoint ton salon vocal et lance la radio")
async def cmd_play(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "❌ Tu dois être dans un salon vocal.", ephemeral=True
        )
        return
    await interaction.response.defer()
    ok, info = await player.play(interaction.user.voice.channel)
    if ok:
        np = await player.fetch_now_playing()
        await interaction.followup.send("▶️ Radio lancée !", embed=np_embed(np))
    else:
        await interaction.followup.send(f"❌ {info}")


@radio_group.command(name="stop", description="Arrête la radio et quitte le salon vocal")
async def cmd_stop(interaction: discord.Interaction):
    await player.stop()
    await interaction.response.send_message("⏹️ Radio arrêtée.")


@radio_group.command(name="volume", description="Règle le volume (0 à 200 %)")
@app_commands.describe(niveau="Volume en % — 100 = normal, 200 = amplifié ×2")
async def cmd_volume(interaction: discord.Interaction, niveau: int):
    niveau = max(0, min(200, niveau))
    player.volume = niveau / 100.0
    await interaction.response.defer()
    await player.restart_with_volume()
    await interaction.followup.send(f"🔊 Volume : **{niveau} %**")


@radio_group.command(name="np", description="Affiche le titre en cours sur la radio")
async def cmd_np(interaction: discord.Interaction):
    await interaction.response.defer()
    np = await player.fetch_now_playing()
    if not np:
        await interaction.followup.send("❌ Impossible de contacter AzuraCast.")
        return
    await interaction.followup.send(embed=np_embed(np))


@radio_group.command(name="station", description="Choisis la station à écouter (liste auto-complétée depuis le serveur)")
@app_commands.describe(station="La station à écouter")
async def cmd_station(interaction: discord.Interaction, station: str):
    await interaction.response.defer()
    ok = await player.set_station(station)
    if not ok:
        await interaction.followup.send(f"❌ Station « {station} » injoignable (flux introuvable).")
        return
    np = await player.fetch_now_playing()
    label = (np.get("station", {}).get("name") if np else None) or station
    # NE PAS annoncer un changement de son qui n'a pas eu lieu. Si le bot n'est pas en
    # vocal, il ne joue RIEN : l'ancienne version affichait quand même « Station → X »,
    # on croyait écouter X (on écoutait le site, resté sur la Mainstage) et on votait /
    # blacklistait sur le mauvais titre. Incident du 14/07 : dire la vérité, toujours.
    if not player.is_playing:
        await interaction.followup.send(
            f"📻 Station retenue → **{label}**\n"
            f"⚠️ Mais je ne suis **pas en vocal** : rien ne joue de mon côté. "
            f"Lance `/radio play` pour l'entendre (ou `/radio liens` pour l'écouter ailleurs).",
            embed=np_embed(np))
        return
    await interaction.followup.send(f"📻 Station → **{label}** (son basculé ✅)", embed=np_embed(np))


@radio_group.command(name="liens", description="Les liens mp3 des stations — à coller dans VLC, le tel, ou à partager")
async def cmd_liens(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(
        title="🔗 Liens des stations",
        description="Colle-les dans VLC, un lecteur mobile, ou partage-les.",
        color=0x8B5CF6,
    )
    for sc, (nm, path) in STATION_MP3.items():
        ici = " ← en cours" if sc == player.station else ""
        embed.add_field(name=f"{nm}{ici}", value=f"`{PUBLIC_BASE}{path}`", inline=False)
    await interaction.followup.send(embed=embed)


@cmd_station.autocomplete("station")
async def station_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    stations = await player.fetch_stations()
    return [
        app_commands.Choice(name=nm, value=sc)
        for sc, nm in stations
        if cur in sc.lower() or cur in nm.lower()
    ][:25]


# NB : pas de commande admin (skip/next/…). Le contrôle de l'antenne — passer un
# titre, rejouer, etc. — appartient aux AUDITEURS via le vote sur le site, pas à
# une commande Discord privilégiée. Le bot ne fait qu'écouter et informer.
@radio_group.command(name="vote", description="Voter pour la suite (ENCORE / SKIP) sur le site des auditeurs")
async def cmd_vote(interaction: discord.Interaction):
    if not SITE_URL:
        await interaction.response.send_message(
            "🗳️ Le contrôle de l'antenne (passer un titre, etc.) se fait par le **vote des "
            "auditeurs** sur le site — pas par une commande Discord.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"🗳️ **Ton avis fait tourner l'antenne** — vote ENCORE ou SKIP ici :\n{SITE_URL}",
        ephemeral=True,
    )


@radio_group.command(name="status", description="Affiche le statut du bot radio")
async def cmd_status(interaction: discord.Interaction):
    playing = player.is_playing
    channel_mention = (
        player.voice_client.channel.mention if player.voice_client and player.voice_client.is_connected()
        else "—"
    )
    vol = int(player.volume * 100)
    color = 0x2ECC71 if playing else 0xE74C3C

    embed = discord.Embed(title="📻 Statut Radio Bot", color=color)
    embed.add_field(name="État",    value="▶️ En lecture" if playing else "⏹️ Arrêté", inline=True)
    embed.add_field(name="Salon",   value=channel_mention, inline=True)
    embed.add_field(name="Volume",  value=f"{vol} %", inline=True)
    embed.add_field(name="Station", value=player.station, inline=True)
    embed.add_field(name="AzuraCast", value=AZURACAST_URL, inline=False)
    await interaction.response.send_message(embed=embed)


@radio_group.command(name="pause", description="Met la lecture en pause sans quitter le salon")
async def cmd_pause(interaction: discord.Interaction):
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        await interaction.response.send_message("⏸️ Mis en pause.")
    else:
        await interaction.response.send_message("❌ Le bot ne lit rien.", ephemeral=True)


@radio_group.command(name="resume", description="Reprend la lecture après une pause")
async def cmd_resume(interaction: discord.Interaction):
    if player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        await interaction.response.send_message("▶️ Reprise de la lecture.")
    else:
        await interaction.response.send_message("❌ Rien n'est en pause.", ephemeral=True)


@radio_group.command(name="setnpchannel", description="Active le message 'En ce moment' dans ce salon")
async def cmd_setnpchannel(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ Cette commande doit être utilisée dans un salon texte.", ephemeral=True)
        return
    await interaction.response.defer()
    np = await player.fetch_now_playing()
    if not np:
        await interaction.followup.send("❌ Impossible de contacter AzuraCast.")
        return
    await np_tracker.start(interaction.channel, np)
    if not poll_now_playing.is_running():
        poll_now_playing.start()
    await interaction.followup.send(
        f"✅ Message 'En ce moment' activé dans {interaction.channel.mention}. "
        f"Il se mettra à jour automatiquement à chaque changement de titre.",
        ephemeral=True,
    )


bot.tree.add_command(radio_group)

bot.run(DISCORD_TOKEN, log_handler=None)
PYEOF

# ── Dockerfile ───────────────────────────────────────────────────────────────
cat > "${SRC_DIR}/Dockerfile" << 'DEOF'
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

CMD ["python", "-u", "bot.py"]
DEOF

# ── Build de l'image Docker ───────────────────────────────────────────────────
echo "  → Build de l'image Docker (peut prendre 1-2 min au premier lancement)..."
docker build -t caleope-azuracast-discord-bot:latest "${SRC_DIR}" \
    --label "caleope.app=${CALEOPE_APP_ID}" \
    --label "caleope.version=1.0.0"
echo "  ✓ Image construite : caleope-azuracast-discord-bot:latest"

# ── post-install.txt ─────────────────────────────────────────────────────────
STATION_ID="${CALEOPE_PARAM_AZURACAST_STATION_ID:-radio}"
AZ_URL="${CALEOPE_PARAM_AZURACAST_URL:-}"

NP_CHAN="${CALEOPE_PARAM_NP_CHANNEL_ID:-}"
NP_STATUS="désactivé (configurer via /radio setnpchannel)"
[ -n "${NP_CHAN}" ] && NP_STATUS="actif sur salon ID ${NP_CHAN}"

cat > "${CONFIG_DIR}/post-install.txt" << EOF

  ┌──────────────────────────────────────────────────────────────────────┐
  │              AzuraCast Radio Bot — Installé                          │
  ├──────────────────────────────────────────────────────────────────────┤
  │  Station   : ${STATION_ID}
  │  AzuraCast : ${AZ_URL}
  │  NP live   : ${NP_STATUS}
  │                                                                      │
  │  Commandes Discord :                                                 │
  │    /radio play          → rejoint ton salon et lance la radio        │
  │    /radio stop          → arrête et quitte le salon                  │
  │    /radio volume <n>    → règle le volume (0-200%)                   │
  │    /radio np            → titre en cours (embed)                     │
  │    /radio station <s>   → change de station (auto-complété)          │
  │    /radio vote          → lien vers le vote des auditeurs (site)     │
  │    /radio status        → statut du bot                              │
  │    /radio pause/resume  → pause sans quitter le salon                │
  │    /radio setnpchannel  → active le message live dans ce salon       │
  │                                                                      │
  │  Pas de commande admin (skip/next) : le contrôle de l'antenne        │
  │  appartient aux auditeurs via le vote sur le site (param SITE_URL).  │
  │                                                                      │
  │  Message auto-update :                                               │
  │    Tape /radio setnpchannel dans n'importe quel salon texte.         │
  │    Le bot postera un embed qui se met à jour à chaque changement     │
  │    de titre avec : titre, artiste, album, barre de progression,      │
  │    nombre d'auditeurs, et prochain titre.                            │
  │                                                                      │
  │  Prérequis Discord :                                                 │
  │    • Mode développeur activé (Paramètres → Apparence)                │
  │    • Permissions bot : Connect, Speak, Send Messages, Embed Links    │
  │    • Intents : Voice States (portail développeur)                    │
  │                                                                      │
  │  Logs :                                                              │
  │    caleope logs azuracast-discord-bot                                │
  └──────────────────────────────────────────────────────────────────────┘
EOF

echo "✓ AzuraCast Radio Bot configuré"
