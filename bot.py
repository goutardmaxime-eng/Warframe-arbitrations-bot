"""bot.py - Warframe Arbitrations Discord Bot
==========================================
Envoie une notification toutes les heures UTC dans un channel Discord configuré,
avec les infos de l'Arbitration en cours (carte, faction, type, tier).

Dépendances :
    pip install discord.py aiohttp beautifulsoup4

Variables d'environnement :
    DISCORD_TOKEN  - Token du bot Discord

Fichier de config :
    config.json    - Stocke le channel_id configuré via /setchannel
"""

import os
import json
import time
import asyncio
import logging
import re
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from bs4 import BeautifulSoup

# ─────────────────────────── Logging ────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("warframe-arbys")

# ─────────────────────────── Constantes ─────────────────────────────────────
CONFIG_FILE        = "config.json"
ARBYS_TXT_URL      = "https://browse.wf/arbys.txt"
ARBYS_HTML_URL     = "https://browse.wf/arbys.txt"
WORLDSTATE_URL     = "https://api.warframestat.us/solNodes"
ARBITRATION_URL    = "https://api.warframestat.us/pc/arbitration"
THUMBNAIL_URL      = (
    "https://static.wikia.nocookie.net/warframe/images/3/3e/"
    "ArbitersOfHexisSigil.png"
)
EMBED_COLOR        = 0x00FF00   # Vert
MAX_RETRIES        = 3
RETRY_DELAY        = 5          # secondes entre chaque retry

# ─────────────────────────── Config helpers ──────────────────────────────────

def load_config() -> dict:
    """Charge la config depuis config.json (crée le fichier si absent)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"Impossible de lire {CONFIG_FILE} : {e}")
    return {}


def save_config(data: dict) -> None:
    """Sauvegarde la config dans config.json."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info(f"Config sauvegardée : {data}")


# ─────────────────────────── Fetch helpers ───────────────────────────────────

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str | None:
    """
    GET texte avec retry automatique.
    Retourne le contenu ou None en cas d'échec.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                text = await resp.text()
                log.debug(f"[fetch_text] {url} → {resp.status} (tentative {attempt})")
                return text
        except Exception as e:
            log.warning(f"[fetch_text] Tentative {attempt}/{MAX_RETRIES} échouée pour {url} : {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    log.error(f"[fetch_text] Impossible de récupérer {url} après {MAX_RETRIES} tentatives.")
    return None


async def fetch_json(session: aiohttp.ClientSession, url: str) -> dict | list | None:
    """
    GET JSON avec retry automatique.
    Retourne le JSON parsé ou None en cas d'échec.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                log.debug(f"[fetch_json] {url} → {resp.status} (tentative {attempt})")
                return data
        except Exception as e:
            log.warning(f"[fetch_json] Tentative {attempt}/{MAX_RETRIES} échouée pour {url} : {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    log.error(f"[fetch_json] Impossible de récupérer {url} après {MAX_RETRIES} tentatives.")
    return None


# ─────────────────────────── Parsing ─────────────────────────────────────────

def parse_node_id_from_txt(txt_content: str) -> str | None:
    """
    Parse arbys.txt pour trouver le node_id correspondant à l'heure actuelle (UTC).
    Format attendu : timestamp_unix,SolNodeXXX
    """
    current_hour_start = int(time.time() // 3600) * 3600
    log.info(f"[parse_node] Heure courante (epoch) : {current_hour_start}")

    for line in txt_content.strip().splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        parts = line.split(",", 1)
        try:
            ts = int(parts[0].strip())
            node_id = parts[1].strip()
            if ts == current_hour_start:
                log.info(f"[parse_node] Node trouvé : {node_id} pour ts={ts}")
                return node_id
        except (ValueError, IndexError):
            continue

    log.warning(f"[parse_node] Aucun node trouvé pour ts={current_hour_start}")
    return None


def parse_tier_from_html(txt_content: str) -> str:
    """
    Extrait le tier depuis arbys.txt en cherchant autour de l'heure actuelle.
    Format : timestamp,SolNodeXXX (S tier) ou similaire
    """
    current_hour_start = int(time.time() // 3600) * 3600
    for line in txt_content.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(",", 1)
        try:
            ts = int(parts[0].strip())
            if ts == current_hour_start:
                match = re.search(r"\(([A-F])\s*tier\)", line, re.IGNORECASE)
                if match:
                    return match.group(1).upper()
        except (ValueError, IndexError):
            continue
    return "Inconnu"


def extract_node_info(worldstate: dict, node_id: str) -> dict | None:
    node = worldstate.get(node_id)
    if node:
        full_name = node.get("value", node_id)
        # Extrait "Callisto" et "Jupiter" depuis "Callisto (Jupiter)"
        if " (" in full_name:
            node_name = full_name.split(" (")[0]
            planet = full_name.split(" (")[1].replace(")", "")
        else:
            node_name = full_name
            planet = "Inconnu"
        return {
            "planet":       planet,
            "node_name":    node_name,
            "mission_type": node.get("type", "Inconnu"),
            "faction":      node.get("enemy", "Inconnu"),
        }
    log.warning(f"[extract_node] Node {node_id!r} introuvable.")
    return None

def calculate_tier(mission_type: str, node_name: str) -> str:
    """
    Calcule le tier basé sur la carte et le type de mission.
    Basé sur la communauté Warframe Arbitrations (Vitus Essence/heure).
    S = Meilleures cartes fermées Défense/Interception
    A = Bonnes cartes Survie/Disruption
    B = Cartes correctes
    C = Cartes moyennes
    D = Mauvaises cartes
    F = Pires cartes
    """
    node = node_name.upper()
    mtype = mission_type.upper()

    # ── S TIER ── Meilleures cartes fermées (spawns contrôlés, max VE/h)
    S_NODES = [
        "AKKAD",        # Eris - Defense - meilleure carte défense
        "HYDRON",       # Sedna - Defense - 2ème meilleure défense
        "STÖFLER",      # Lua - Defense
        "CERBERUS",     # Pluto - Defense
        "BEREHYNIA",    # Sedna - Interception - meilleure interception
        "UR",           # Uranus - Interception
        "LOST PASSAGE", # Lua - Interception
        "ADRASTEA",     # Jupiter - Interception
        "CARACOL",      # Saturn - Interception
    ]

    # ── A TIER ── Bonnes cartes (bons spawns mais moins optimales)
    A_NODES = [
        "ZABALA",       # Eris - Survival
        "HIERACON",     # Pluto - Excavation (très bonne)
        "GABII",        # Ceres - Survival
        "APOLLO",       # Lua - Disruption
        "HOREND",       # Eris - Defection (acceptable)
        "MOT",          # Void - Survival
        "YURSA",        # Neptune - Defense
        "PALUS",        # Pluto - Survival
        "CINXIA",       # Ceres - Interception
        "TESSERA",      # Venus - Defense
    ]

    # ── B TIER ── Cartes correctes
    B_NODES = [
        "ODIN",         # Mercury - Defense
        "HELENE",       # Saturn - Defense
        "CASTA",        # Ceres - Defense
        "BODE",         # Ceres - Excavation
        "MALVA",        # Venus - Excavation
        "LARES",        # Mercury - Defense
        "WAHIBA",       # Mars - Survival
        "CAMERIA",      # Jupiter - Survival
        "SINAI",        # Mars - Defense
    ]

    # ── C TIER ── Cartes moyennes
    C_NODES = [
        "TITAN",        # Saturn - Survival
        "OPHELIA",      # Uranus - Survival
        "CALYPSO",      # Saturn - Survival
        "CYTHEREAN",    # Venus - Interception
        "HYENA",        # Neptune - Interception
        "OUTER TERMINUS", # Pluto - Defense
    ]

    # ── D TIER ── Mauvaises cartes (open tiles, spawns éparpillés)
    D_NODES = [
        "UNDERTOW",     # Uranus - Infested Salvage
        "DIONE",        # Saturn - Defection
        "DESDEMONA",    # Uranus - Defection
        "TORMENT",      # Sedna - Defection
        "CARACOL",      # Saturn - Disruption (open map)
    ]

    # Vérifie S tier
    for n in S_NODES:
        if n in node:
            return "S"

    # Vérifie A tier
    for n in A_NODES:
        if n in node:
            return "A"

    # Vérifie B tier
    for n in B_NODES:
        if n in node:
            return "B"

    # Vérifie C tier
    for n in C_NODES:
        if n in node:
            return "C"

    # Vérifie D tier
    for n in D_NODES:
        if n in node:
            return "D"

    # Fallback basé sur le type de mission
    if "INTERCEPTION" in mtype or "DEFENSE" in mtype:
        return "B"
    if "SURVIVAL" in mtype or "DISRUPTION" in mtype:
        return "C"
    if "EXCAVATION" in mtype:
        return "C"
    return "F"

# ─────────────────────────── Données Arbitration ─────────────────────────────

async def get_current_arbitration() -> dict:
    """
    Récupère et assemble toutes les données de l'Arbitration courante.

    Retourne un dict :
        {
            "carte":   "Stöfler, Lua",
            "faction": "Grineer",
            "type":    "Defense",
            "tier":    "C",
        }
    En cas d'erreur partielle, des valeurs fallback sont utilisées.
    """
    result = {
        "carte":   "Inconnue",
        "faction": "Inconnue",
        "type":    "Inconnu",
        "tier":    "Inconnu",
    }

    async with aiohttp.ClientSession() as session:
        # 1. Récupère arbys.txt pour le node_id
        txt = await fetch_text(session, ARBYS_TXT_URL)
        node_id = None
        if txt:
            node_id = parse_node_id_from_txt(txt)
        else:
            log.error("Impossible de récupérer arbys.txt")

        # 2. Récupère le worldstate pour les infos du node
        if node_id:
            worldstate = await fetch_json(session, WORLDSTATE_URL)
            if worldstate:
                node_info = extract_node_info(worldstate, node_id)
                if node_info:
                    result["carte"]   = f"{node_info['node_name']}, {node_info['planet']}"
                    result["faction"] = node_info["faction"]
                    result["type"]    = node_info["mission_type"]
                else:
                    log.warning(f"Infos introuvables pour node_id={node_id}")
            else:
                log.error("Impossible de récupérer le worldstate.")
        else:
            log.warning("node_id non trouvé, les infos de mission seront incomplètes.")

        # 3. Calcule le tier depuis le type de mission
        if result["type"] != "Inconnu":
            result["tier"] = calculate_tier(result["type"], result["carte"])

    log.info(f"[get_arbitration] Résultat : {result}")
    return result


# ─────────────────────────── Embed ───────────────────────────────────────────

def build_embed(data: dict) -> discord.Embed:
    """Construit l'embed Discord stylé pour l'Arbitration."""
    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    embed = discord.Embed(
        title="🗡️ Nouvelle Arbitration !",
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.add_field(name="🗺️ Carte",    value=data["carte"],   inline=False)
    embed.add_field(name="⚔️ Faction",  value=data["faction"], inline=True)
    embed.add_field(name="🎯 Type",     value=data["type"],    inline=True)
    embed.add_field(name="🏅 Tier",     value=data["tier"],    inline=True)
    embed.set_footer(text=f"Source: browse.wf | UTC • {now_utc}")
    return embed


# ─────────────────────────── Cog principal ───────────────────────────────────

class ArbitrationsCog(commands.Cog):
    """Cog gérant les notifications d'Arbitration et la commande /setchannel."""

    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self.config = load_config()
        log.info(f"[Cog] Config chargée : {self.config}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        log.info(f"[on_ready] Bot connecté en tant que {self.bot.user} (ID: {self.bot.user.id})")

        # Synchronise les slash commands globalement
        try:
            synced = await self.bot.tree.sync()
            log.info(f"[on_ready] {len(synced)} slash command(s) synchronisée(s).")
        except Exception as e:
            log.error(f"[on_ready] Erreur sync slash commands : {e}")

        # Envoi immédiat de l'Arbitration actuelle
        await self._notify_now("Démarrage du bot")

        # Démarre la loop horaire (si pas déjà démarrée)
        if not self.hourly_loop.is_running():
            self.hourly_loop.start()

    # ── Loop horaire ──────────────────────────────────────────────────────────

    @tasks.loop(seconds=3600)
    async def hourly_loop(self):
        """Se déclenche toutes les 3600 secondes, avec alignement sur H:00 UTC."""
        log.info("[hourly_loop] Tick horaire — envoi de la notification.")
        await self._notify_now("Loop horaire")

    @hourly_loop.before_loop
    async def before_hourly_loop(self):
        """
        Attend que le bot soit prêt, puis calcule le délai jusqu'à la prochaine
        heure pile UTC pour aligner la loop.
        """
        await self.bot.wait_until_ready()

        now     = time.time()
        # Secondes écoulées depuis le début de l'heure courante
        elapsed = now % 3600
        # Secondes à attendre pour atteindre la prochaine heure pile
        delay   = 3600 - elapsed

        next_dt = datetime.fromtimestamp(now + delay, tz=timezone.utc)
        log.info(f"[before_loop] Prochaine notification à {next_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} "
                 f"(dans {delay:.0f}s)")
        await asyncio.sleep(delay)

    # ── Envoi notification ────────────────────────────────────────────────────

    async def _notify_now(self, reason: str = ""):
        """Récupère l'Arbitration et envoie les embeds dans le channel configuré."""
        channel_id = self.config.get("channel_id")
        if not channel_id:
            log.warning("[notify] Aucun channel configuré. Utilisez /setchannel d'abord.")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            log.error(f"[notify] Channel {channel_id} introuvable.")
            return

        log.info(f"[notify] Envoi ({reason}) dans #{channel.name} ({channel_id})")
        try:
            # Embed 1 : Arbitration actuelle
            data  = await get_current_arbitration()
            embed = build_embed(data)

            # Embed 2 : 3 prochaines tier S
            async with aiohttp.ClientSession() as session:
                txt        = await fetch_text(session, ARBYS_TXT_URL)
                worldstate = await fetch_json(session, WORLDSTATE_URL)

            next_s_embed = None
            if txt and worldstate:
                current_hour_start = int(time.time() // 3600) * 3600
                future_nodes = []
                for line in txt.strip().splitlines():
                    line = line.strip()
                    if not line or "," not in line:
                        continue
                    parts = line.split(",", 1)
                    try:
                        ts      = int(parts[0].strip())
                        node_id = parts[1].strip()
                        if ts > current_hour_start:
                            future_nodes.append((ts, node_id))
                    except (ValueError, IndexError):
                        continue

                future_nodes.sort(key=lambda x: x[0])

                s_tier_results = []
                for ts, node_id in future_nodes:
                    if len(s_tier_results) >= 3:
                        break
                    node_info = extract_node_info(worldstate, node_id)
                    if not node_info:
                        continue
                    tier = calculate_tier(node_info["mission_type"], node_info["node_name"])
                    if tier == "S":
                        s_tier_results.append({
                            "ts":      ts,
                            "carte":   f"{node_info['node_name']}, {node_info['planet']}",
                            "faction": node_info["faction"],
                            "type":    node_info["mission_type"],
                        })

                if s_tier_results:
                    next_s_embed = discord.Embed(
                        title="⭐ Prochaines Tier S",
                        color=0xFFD700,
                        timestamp=datetime.now(timezone.utc),
                    )
                    for i, arby in enumerate(s_tier_results, 1):
                        discord_ts = f"<t:{arby['ts']}:R> (<t:{arby['ts']}:t> UTC)"
                        next_s_embed.add_field(
                            name=f"#{i} — {arby['carte']}",
                            value=(
                                f"🕐 {discord_ts}\n"
                                f"⚔️ {arby['faction']} • 🎯 {arby['type']}"
                            ),
                            inline=False,
                        )
                    next_s_embed.set_footer(text="Source: browse.wf | UTC")

            # Envoie les deux embeds ensemble
            embeds = [embed]
            if next_s_embed:
                embeds.append(next_s_embed)
            await channel.send(embeds=embeds)
            log.info(f"[notify] Embeds envoyés : {data}")

        except discord.Forbidden:
            log.error(f"[notify] Permissions insuffisantes pour écrire dans #{channel.name}.")
        except discord.HTTPException as e:
            log.error(f"[notify] Erreur HTTP Discord : {e}")
        except Exception as e:
            log.error(f"[notify] Erreur inattendue : {e}", exc_info=True)

    # ── Slash command /setchannel ─────────────────────────────────────────────

    @app_commands.command(
        name="setchannel",
        description="Définit le channel où les notifications d'Arbitration seront envoyées.",
    )
    @app_commands.describe(channel="Le channel texte cible")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchannel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        """Configure le channel de notifications et sauvegarde dans config.json."""
        self.config["channel_id"] = channel.id
        save_config(self.config)

        log.info(f"[setchannel] Channel configuré : #{channel.name} ({channel.id}) "
                 f"par {interaction.user} ({interaction.user.id})")

        await interaction.response.send_message(
            f"✅ Les notifications d'Arbitration seront désormais envoyées dans {channel.mention}.",
            ephemeral=True,
        )

    @setchannel.error
    async def setchannel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission `Gérer le serveur` pour utiliser cette commande.",
                ephemeral=True,
            )
        else:
            log.error(f"[setchannel_error] {error}", exc_info=True)
            await interaction.response.send_message(
                "❌ Une erreur inattendue s'est produite.",
                ephemeral=True,
            )
    @app_commands.command(    
        name="nexts",        
        description="Affiche les 3 prochaines Arbitrations de tier S.",
    )
    async def nexts(self, interaction: discord.Interaction):
        """Cherche les 3 prochaines cartes tier S dans le schedule."""
        await interaction.response.defer()

        try:
            async with aiohttp.ClientSession() as session:
                txt = await fetch_text(session, ARBYS_TXT_URL)

            if not txt:
                await interaction.followup.send("❌ Impossible de récupérer le schedule.", ephemeral=True)
                return

            now = int(time.time())
            current_hour_start = int(now // 3600) * 3600

            future_nodes = []
            for line in txt.strip().splitlines():
                line = line.strip()
                if not line or "," not in line:
                    continue
                parts = line.split(",", 1)
                try:
                    ts = int(parts[0].strip())
                    node_id = parts[1].strip()
                    if ts >= current_hour_start:
                        future_nodes.append((ts, node_id))
                except (ValueError, IndexError):
                    continue

            future_nodes.sort(key=lambda x: x[0])

            async with aiohttp.ClientSession() as session:
                worldstate = await fetch_json(session, WORLDSTATE_URL)

            if not worldstate:
                await interaction.followup.send("❌ Impossible de récupérer le worldstate.", ephemeral=True)
                return

            s_tier_results = []
            for ts, node_id in future_nodes:
                if len(s_tier_results) >= 3:
                    break
                node_info = extract_node_info(worldstate, node_id)
                if not node_info:
                    continue
                tier = calculate_tier(node_info["mission_type"], node_info["node_name"])
                if tier == "S":
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    s_tier_results.append({
                        "ts":      ts,
                        "dt":      dt,
                        "carte":   f"{node_info['node_name']}, {node_info['planet']}",
                        "faction": node_info["faction"],
                        "type":    node_info["mission_type"],
                    })

            if not s_tier_results:
                await interaction.followup.send("😔 Aucune Arbitration tier S trouvée dans les prochaines heures.")
                return

            embed = discord.Embed(
                title="⭐ Prochaines Arbitrations Tier S",
                color=0xFFD700,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=THUMBNAIL_URL)

            for i, arby in enumerate(s_tier_results, 1):
                discord_ts = f"<t:{arby['ts']}:R> (<t:{arby['ts']}:t> UTC)"
                embed.add_field(
                    name=f"#{i} — {arby['carte']}",
                    value=(
                        f"🕐 {discord_ts}\n"
                        f"⚔️ {arby['faction']} • 🎯 {arby['type']}"
                    ),
                    inline=False,
                )

            embed.set_footer(text="Source: browse.wf | UTC")
            await interaction.followup.send(embed=embed)
            log.info(f"[nexts] {len(s_tier_results)} résultats envoyés à {interaction.user}")

        except Exception as e:
            log.error(f"[nexts] Erreur : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur inattendue s'est produite.", ephemeral=True)

    


# ─────────────────────────── Bot setup ───────────────────────────────────────

intents         = discord.Intents.default()
intents.guilds  = True      # Nécessaire pour récupérer les channels

bot = commands.Bot(command_prefix="!", intents=intents)


async def main():
    """Point d'entrée asynchrone : charge le cog et démarre le bot."""
    async with bot:
        await bot.add_cog(ArbitrationsCog(bot))
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            raise RuntimeError(
                "La variable d'environnement DISCORD_TOKEN n'est pas définie.\n"
                "Exemple : export DISCORD_TOKEN=ton_token_ici"
            )
        log.info("[main] Démarrage du bot Warframe Arbitrations…")
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
