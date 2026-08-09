import { parse, stringify } from 'yaml'
import { Client as FClient, Events as FEvents, Webhook, PermissionFlags } from '@fluxerjs/core'
import { Client as DClient, Events as DEvents, GatewayIntentBits, PermissionsBitField} from 'discord.js'
import { joinVoiceChannel, createAudioPlayer, createAudioResource, AudioPlayerStatus, VoiceConnectionStatus, getVoiceConnection, StreamType, entersState } from '@discordjs/voice'
import { readFile, writeFile, access, unlink } from 'fs/promises'
import { execFile, spawn } from 'child_process'
import { promisify } from 'util'
import * as disc from './lib/disc_funcs.js'
import * as flux from './lib/flux_funcs.js'
import * as cmd from './lib/cmds.js'

const execFileAsync = promisify(execFile)

const PREFIX = process.env.CMD_PREFIX ?? 'brdg;'
if (!process.env.FLUXER_TOKEN || !process.env.DISCORD_TOKEN) {
    throw new Error("One or more tokens missing! Please set them in your environment variables.", {cause: 'MISSING_TOKENS'})
}

const fluxBot = new FClient({ intents: 0 });
const discBot = new DClient({ intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.GuildVoiceStates,
]});

let bridges;
try {
    const bridgefile = parse(await readFile('./db/Bridges.yaml', 'utf8'), {schema: 'failsafe'}); console.log(`Bridges loaded!`)
    bridges = ('Discord' in bridgefile && 'Fluxer' in bridgefile)? bridgefile : {Discord: {}, Fluxer: {}};
    writeFile('./db/Bridges.yaml', stringify(bridges));
}
catch {
    console.log("Error finding './db/Bridges.yaml.\nAttempting to create one now");
    bridges = {Discord: {}, Fluxer: {}};
    writeFile('./db/Bridges.yaml', stringify(bridges))
}

fluxBot.once('ready', () => console.log(`Fluxer logged in as ${fluxBot.user.username}#${fluxBot.user.discriminator}`));

fluxBot.on('messageCreate', async (msg) => {
     if (msg.content.startsWith(PREFIX) && !msg.author.bot) {
        const stripped = msg.content.replace(PREFIX, "");
        const mem = await msg.guild.members.get(msg.author.id)
        const authed = mem.permissions.has(PermissionFlags.ManageChannels);
        let res = cmd.parse(authed, bridges, 'Fluxer', msg.channel.id, stripped);
        if (typeof res == 'object') {
            bridges = res;
            writeFile('./db/Bridges.yaml', stringify(bridges))
            msg.react('👍')
        }
        else {
            res = res.replaceAll("[PRFX]", PREFIX)
            msg.channel.send(res)
        }
        return;
    }
    const rawAttachments = await flux.get_flux_attachments(msg);
    if (msg.author.bot || (!msg.content && rawAttachments.size == 0)) {return};
    if (msg.channelId in bridges.Fluxer) {
        for(const ID of bridges.Fluxer[msg.channelId]) {
            const discChannel = await discBot.channels.fetch(ID);
            const discGuild = await discBot.guilds.fetch(discChannel.guildId);
            const rawMsg = await flux.get_flux_content(msg, msg.referencedMessage, discGuild)
            const guildHook = await disc.get_disc_hook(discBot, discChannel);
            guildHook.send({
                username: msg.author.globalName,
                avatarURL: `https://fluxerusercontent.com/avatars/${msg.author.id}/${msg.author.avatar}.webp`,
                content: rawMsg,
                files: rawAttachments
            })
        }
    }
})

// DISCORD / FLUXER BOT DIVIDER FOR RILLABEL EASY READING

discBot.on('clientReady', async (data) => {
    console.log(`Discord logged in as ${data.user.tag}!`)
    // Après un reset(), tenter de rejoindre le dernier salon parle actif
    if (parleChannelId && parleGuildId) {
        console.log('[parle] restauration après reset, rejoin dans 5s...')
        await new Promise(r => setTimeout(r, 5000))
        try {
            const guild = await discBot.guilds.fetch(parleGuildId)
            const channel = await guild.channels.fetch(parleChannelId)
            const conn = joinVoiceChannel({
                channelId: channel.id,
                guildId: guild.id,
                adapterCreator: guild.voiceAdapterCreator,
                selfDeaf: false,
            })
            await entersState(conn, VoiceConnectionStatus.Ready, 15_000)
            const player = createAudioPlayer()
            conn.subscribe(player)
            conn.on(VoiceConnectionStatus.Disconnected, async () => {
                try {
                    await Promise.race([
                        entersState(conn, VoiceConnectionStatus.Signalling, 5_000),
                        entersState(conn, VoiceConnectionStatus.Connecting, 5_000),
                    ])
                    await entersState(conn, VoiceConnectionStatus.Ready, 30_000)
                } catch {
                    try { conn.rejoin(); await entersState(conn, VoiceConnectionStatus.Ready, 30_000) }
                    catch { parleActive = false; parleChannelId = null; try { conn.destroy() } catch {} }
                }
            })
            talkLoop(conn, player).catch(() => { try { conn.destroy() } catch {} })
            console.log('[parle] restauration OK')
        } catch (e) {
            console.error('[parle] restauration échouée:', e.message)
            parleActive = false
            parleChannelId = null
        }
    }
});

// ── Keep-alive vocal ────────────────────────────────────────────────────────
// Quand le dernier humain quitte un salon, le bot le rejoint pour maintenir
// le timer d'occupation. Il repart dès que quelqu'un revient.

const keepAliveChannels = new Map() // guildId -> channelId

discBot.on('voiceStateUpdate', async (oldState, newState) => {
    // Ignorer les mouvements du bot lui-même
    if (oldState.member?.user?.bot || newState.member?.user?.bot) return

    const guildId = oldState.guild?.id ?? newState.guild?.id
    if (!guildId) return

    // Quelqu'un a rejoint un salon où le bot est en keep-alive → il "parle" 10s puis quitte
    if (newState.channelId && keepAliveChannels.get(guildId) === newState.channelId) {
        // Ne pas interférer si brdg;parle est actif dans ce guild
        if (parleActive && parleGuildId === guildId) return

        console.log(`[keep-alive] humain rejoint, lancement blague 10s`)
        keepAliveChannels.delete(guildId)

        // Détruire la connexion muette et en créer une non-muette pour jouer le son
        try { getVoiceConnection(guildId)?.destroy() } catch {}
        await new Promise(r => setTimeout(r, 400))

        let conn
        try {
            conn = joinVoiceChannel({
                channelId: newState.channelId,
                guildId,
                adapterCreator: newState.guild.voiceAdapterCreator,
                selfDeaf: false,
            })
            await entersState(conn, VoiceConnectionStatus.Ready, 8_000)
        } catch (e) {
            console.error('[keep-alive] connexion blague échouée:', e.message)
            try { conn?.destroy() } catch {}
            return
        }

        const player = createAudioPlayer()
        conn.subscribe(player)

        let keepAliveRunning = true
        const deadline = Date.now() + 10_000
        ;(async () => {
            while (keepAliveRunning && Date.now() < deadline) {
                const variant = SPEED_FILES[Math.floor(Math.random() * SPEED_FILES.length)]
                try { await playFile(player, variant.path) } catch { break }
                await new Promise(r => setTimeout(r, 80 + Math.random() * 400))
            }
            keepAliveRunning = false
            try { conn.destroy() } catch {}
        })().catch(() => { try { conn?.destroy() } catch {} })
        return
    }

    // Quelqu'un a quitté un salon
    if (!oldState.channelId || oldState.channelId === newState.channelId) return
    const channel = oldState.channel
    if (!channel) return

    const humanCount = channel.members.filter(m => !m.user.bot).size
    if (humanCount > 0) return // il reste des humains

    // Si brdg;parle est actif dans ce guild, ne pas toucher à sa connexion
    if (parleActive && parleGuildId === guildId) return

    // Salon vide d'humains : le bot rejoint pour tenir le timer
    const existingConn = getVoiceConnection(guildId)
    if (existingConn) existingConn.destroy()

    try {
        const conn = joinVoiceChannel({
            channelId: channel.id,
            guildId,
            adapterCreator: channel.guild.voiceAdapterCreator,
            selfDeaf: true,
            selfMute: true,
        })
        keepAliveChannels.set(guildId, channel.id)
        console.log(`[keep-alive] bot rejoint ${channel.name} (${channel.id})`)

        conn.on(VoiceConnectionStatus.Disconnected, () => {
            keepAliveChannels.delete(guildId)
        })
    } catch (e) {
        console.error('[keep-alive] erreur join:', e.message)
    }
})

// ── Easter egg : brdg;parle ─────────────────────────────────────────────────
// Le papa de kiki essaie de parler mais n'a qu'un seul mot dans son vocabulaire.
// Le son est téléchargé et traité au premier appel (lazy), puis mis en cache.

const DB_DIR = '/Bot/db'
const AUDIO_PATH = `${DB_DIR}/augghh.mp3`
const RAW_PATH = '/tmp/augghh_raw.wav'

// Variantes de vitesse pré-générées — jouées directement (createAudioResource(path) = fiable)
// Chemins explicites pour éviter String(1.0)="1" ou String(2.0)="2" en JS
const SPEED_FILES = [
    { speed: 0.55, path: `${DB_DIR}/augghh_055.mp3` },
    { speed: 0.7,  path: `${DB_DIR}/augghh_07.mp3`  },
    { speed: 0.85, path: `${DB_DIR}/augghh_085.mp3` },
    { speed: 1.0,  path: `${DB_DIR}/augghh.mp3`     },
    { speed: 1.2,  path: `${DB_DIR}/augghh_12.mp3`  },
    { speed: 1.5,  path: `${DB_DIR}/augghh_15.mp3`  },
    { speed: 1.8,  path: `${DB_DIR}/augghh_18.mp3`  },
    { speed: 2.0,  path: `${DB_DIR}/augghh_20.mp3`  },
]

const FILTER = [
    '[0:a]atrim=start=0:end=0.13,asetpts=PTS-STARTPTS[a1]',
    'aevalsrc=0:d=0.07[sil1]',
    '[0:a]atrim=start=0:end=0.13,asetpts=PTS-STARTPTS[a2]',
    'aevalsrc=0:d=0.05[sil2]',
    '[0:a]atrim=start=0:end=0.08,asetpts=PTS-STARTPTS[a3]',
    '[0:a]atrim=start=0:end=0.08,asetpts=PTS-STARTPTS[a4]',
    'aevalsrc=0:d=0.09[sil3]',
    '[0:a]atrim=start=0:end=0.35,asetpts=PTS-STARTPTS[a5]',
    'aevalsrc=0:d=0.06[sil4]',
    '[0:a]atrim=start=0:end=0.08,asetpts=PTS-STARTPTS[a6]',
    '[0:a]atrim=start=0:end=0.22,asetpts=PTS-STARTPTS[a7]',
    'aevalsrc=0:d=0.08[sil5]',
    '[0:a]atrim=start=0:end=2.0,asetpts=PTS-STARTPTS[a8]',
    '[a1][sil1][a2][sil2][a3][a4][sil3][a5][sil4][a6][a7][sil5][a8]concat=n=13:v=0:a=1[out]',
].join(';')

async function generateAudio() {
    console.log('[easter egg] téléchargement du son...')
    await execFileAsync('yt-dlp', [
        '-x', '--audio-format', 'wav', '--audio-quality', '0',
        '--no-playlist', '-o', RAW_PATH,
        'https://www.youtube.com/watch?v=gft2w1d6gZE',
    ])
    console.log('[easter egg] traitement ffmpeg...')
    await execFileAsync('ffmpeg', [
        '-i', RAW_PATH, '-filter_complex', FILTER,
        '-map', '[out]', '-ar', '44100', '-b:a', '128k', AUDIO_PATH,
    ])
    // Générer les variantes de vitesse
    for (const { speed, path } of SPEED_FILES) {
        await execFileAsync('ffmpeg', [
            '-y', '-i', AUDIO_PATH,
            '-af', `atempo=${speed}`,
            '-ar', '44100', '-b:a', '128k', path,
        ])
        console.log(`[easter egg] variante ${speed}x générée`)
    }
    try { await unlink(RAW_PATH) } catch {}
    console.log('[easter egg] son prêt :', AUDIO_PATH)
}

let audioGenerating = false
let parleActive = false
let parleGuildId = null
let parleChannelId = null
let parleVolume = 1.0  // 1.0 = 100%, range 0.0–2.0

function playFile(player, path) {
    return new Promise((resolve, reject) => {
        // ffmpeg applique le volume et sort en OggOpus → prism-media ne ré-encode pas
        const ff = spawn('ffmpeg', [
            '-re', '-i', path,
            '-af', `volume=${parleVolume}`,
            '-c:a', 'libopus', '-f', 'ogg', 'pipe:1',
        ], { stdio: ['ignore', 'pipe', 'ignore'] })
        ff.on('error', e => { console.error('[parle] ffmpeg error:', e.message); reject(e) })

        const resource = createAudioResource(ff.stdout, { inputType: StreamType.OggOpus })
        const onIdle  = () => { player.off('error', onError); try { ff.kill() } catch {}; resolve() }
        const onError = (e) => { player.off(AudioPlayerStatus.Idle, onIdle); try { ff.kill() } catch {}; console.error('[parle] player error:', e.message); reject(e) }
        player.once(AudioPlayerStatus.Idle, onIdle)
        player.once('error', onError)
        player.play(resource)
    })
}

async function talkLoop(connection, player) {
    parleActive = true

    let errorCount = 0
    while (parleActive) {
        const variant = SPEED_FILES[Math.floor(Math.random() * SPEED_FILES.length)]
        try {
            await playFile(player, variant.path)
            errorCount = 0
        } catch (e) {
            errorCount++
            console.log(`[parle] playFile error (${errorCount}/3):`, e?.message)
            if (errorCount >= 3) { console.log('[parle] trop d\'erreurs, sortie'); break }
            await new Promise(r => setTimeout(r, 2000))
            continue
        }

        if (!parleActive) break

        // Pause variable : courte entre deux "syllabes", longue entre "phrases"
        const pause = Math.random() < 0.25
            ? 800 + Math.random() * 2000   // pause longue (entre phrases)
            : 80 + Math.random() * 400     // pause courte (entre syllabes)
        await new Promise(r => setTimeout(r, pause))
    }

    parleActive = false
    parleGuildId = null
    parleChannelId = null
    try { connection.destroy() } catch {}
    console.log('[easter egg] talkLoop terminé')
}

async function handleParle(msg) {
    const voiceChannel = msg.member?.voice?.channel;
    if (!voiceChannel) {
        msg.channel.send('AUGGH ?');
        return;
    }

    // Couper le keep-alive ou une session parle déjà active
    parleActive = false
    try {
        const existingConn = getVoiceConnection(msg.guild.id)
        if (existingConn) existingConn.destroy()
    } catch (e) { console.error('[parle] destroy existingConn:', e.message) }
    keepAliveChannels.delete(msg.guild.id)

    // Laisser le temps à la lib de nettoyer la connexion précédente
    await new Promise(r => setTimeout(r, 600))

    // Vérifier si le fichier son est là
    let hasAudio = false;
    try { await access(AUDIO_PATH); hasAudio = true; } catch {}
    console.log('[parle] hasAudio:', hasAudio, '| voiceChannel:', voiceChannel.id)

    if (!hasAudio) {
        if (audioGenerating) { msg.channel.send('*AUGGH... (patience)*'); return; }
        audioGenerating = true;
        const statusMsg = await msg.channel.send('*...* 🎵').catch(() => null);
        try {
            await generateAudio();
            hasAudio = true;
        } catch (e) {
            console.error('[easter egg] génération échouée:', e.message);
            statusMsg?.edit('AUGGH. *(son indisponible)*').catch(() => {});
            audioGenerating = false;
            return;
        }
        statusMsg?.delete().catch(() => {});
        audioGenerating = false;
    }

    msg.react('🗣️').catch(() => {});

    let connection;
    try {
        connection = joinVoiceChannel({
            channelId: voiceChannel.id,
            guildId: msg.guild.id,
            adapterCreator: msg.guild.voiceAdapterCreator,
            selfDeaf: true,
        });
        console.log('[parle] joinVoiceChannel OK, attente Ready...')
        await entersState(connection, VoiceConnectionStatus.Ready, 10_000)
        parleGuildId = msg.guild.id
        parleChannelId = voiceChannel.id
        console.log('[parle] connexion Ready')
    } catch (e) {
        console.error('[parle] connexion échouée:', e.message)
        try { connection?.destroy() } catch {}
        msg.channel.send('AUGGH. *(connexion impossible)*')
        return
    }

    const player = createAudioPlayer();
    connection.subscribe(player);

    connection.on(VoiceConnectionStatus.Disconnected, async () => {
        console.log('[parle] Disconnected — tentative reconnexion...')
        try {
            // Soit Discord reconnecte tout seul (réseau bref), soit on force un rejoin
            await Promise.race([
                entersState(connection, VoiceConnectionStatus.Signalling, 5_000),
                entersState(connection, VoiceConnectionStatus.Connecting, 5_000),
            ])
            await entersState(connection, VoiceConnectionStatus.Ready, 30_000)
            console.log('[parle] reconnexion automatique OK')
        } catch {
            try {
                console.log('[parle] tentative rejoin...')
                connection.rejoin()
                await entersState(connection, VoiceConnectionStatus.Ready, 30_000)
                console.log('[parle] rejoin OK')
            } catch {
                console.log('[parle] reconnexion définitivement échouée, on lâche')
                parleActive = false
                try { connection.destroy() } catch {}
            }
        }
    });

    // Lancer la boucle de "parole" — ne bloque pas, tourne en arrière-plan
    talkLoop(connection, player).catch(e => {
        console.error('[easter egg] talkLoop error:', e.message)
        parleActive = false
        try { connection.destroy() } catch {}
    })
}

// ───────────────────────────────────────────────────────────────────────────

discBot.on('messageCreate', async (msg) => {
    if (msg.content.startsWith(PREFIX) && !msg.author.bot) {
        const stripped = msg.content.replace(PREFIX, '').trim();

        // Easter egg — doit être capturé AVANT cmd.parse (commande inconnue sinon)
        if (stripped === 'parle') {
            handleParle(msg).catch(console.error);
            return;
        }

        if (stripped === 'tais') {
            parleActive = false
            parleGuildId = null
            parleChannelId = null
            const conn = getVoiceConnection(msg.guild.id)
            if (conn) conn.destroy()
            keepAliveChannels.delete(msg.guild.id)
            msg.react('🤫').catch(() => {})
            return;
        }

        if (stripped.startsWith('volume')) {
            const val = parseInt(stripped.split(/\s+/)[1], 10)
            if (isNaN(val) || val < 0 || val > 200) {
                msg.channel.send(`Volume actuel : **${Math.round(parleVolume * 100)}%** (0–200)`)
            } else {
                parleVolume = val / 100
                msg.react('🔊').catch(() => {})
            }
            return;
        }

        const authed = msg.member.permissions.has(PermissionsBitField.Flags.ManageChannels);
        let res = cmd.parse(authed, bridges, 'Discord', msg.channel.id, stripped);
        if (typeof res == 'object') {
            bridges = res;
            writeFile('./db/Bridges.yaml', stringify(bridges))
            msg.react('👍')
        }
        else {
            res = res.replaceAll("[PRFX]", PREFIX)
            msg.channel.send(res);
        }

        return;
    }
    const rawAttachments = await disc.get_disc_attachments(msg);
    if (msg.author.bot || (!msg.content && rawAttachments.length == 0)) {return}
    if (msg.channelId in bridges.Discord) {
        let replyTo;
        if (msg.reference) {
            replyTo = await msg.fetchReference();
        }
        for (const ID of bridges.Discord[msg.channelId]) {
            const fluxChannel = await fluxBot.channels.fetch(ID);
            const fluxGuild = await fluxBot.guilds.fetch(fluxChannel.guildId)
            const rawContent = await disc.get_disc_content(msg, replyTo, fluxGuild)
            const hook = await flux.get_flux_hook(fluxBot, fluxChannel);
            hook.send({
            username: msg.author.displayName,
            content: rawContent,
            avatar_url: `https://cdn.discordapp.com/avatars/${msg.author.id}/${msg.author.avatar}`,
            files: rawAttachments
            })
        }
    }
})

discBot.login(process.env.DISCORD_TOKEN);
fluxBot.login(process.env.FLUXER_TOKEN);

// ERROR HANDLING DIVIDER FOR RILLABEL EASIER READING

let handling = 0;
async function reset (attempts) {
    const delay = (attempts <= 3)? 0 : attempts - 3;
    try {
        await fluxBot.destroy()
        await discBot.destroy()
        await discBot.login(process.env.DISCORD_TOKEN)
        await fluxBot.login(process.env.FLUXER_TOKEN)
        console.log('Restarted successfully!')
        handling = 0
    }
    catch (e) {
        console.error('Ran into an issue while restarting:', e.message, '\nAttempting again in', delay, 'minutes.');
        ++attempts;
        setTimeout(() => {reset(attempts)}, 60000 * delay)
    }

}

process.on('uncaughtException', async (e) => {
    if (handling == 0) {
        console.error(new Date().toTimeString().match(/\S+/)[0], 'Ran into an error:', e.message, "\nBoth bots will attempt to restart.")
        handling = 1
        setTimeout(() => {reset(0)}, 5000)
    }
})
