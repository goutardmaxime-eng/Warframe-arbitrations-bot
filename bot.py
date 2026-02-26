"""
bot.py - Warframe Arbitrations Discord Bot
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
ARBYS_HTML_URL     = "https://browse.wf/arbys"
WORLDSTATE_URL     = "https://api.warframestat.us/pc/worldstate"
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


def parse_tier_from_html(html_content: str) -> str:
    """
    Parse la page HTML de browse.wf/arbys pour extraire le tier de l'Arbitration actuelle.
    Cherche une ligne contenant l'heure UTC actuelle au format "HHMM •" (ex : "1400 •").
    Retourne "S", "A", "B", "C", "D" ou "F", sinon "Inconnu".
    """
    now_utc_hour = datetime.now(timezone.utc).hour
    hour_str = f"{now_utc_hour:02d}00"
    log.info(f"[parse_tier] Recherche du tier pour {hour_str}")

    try:
        soup = BeautifulSoup(html_content, "html.parser")
        # Cherche dans tout le texte brut de la page
        full_text = soup.get_text(separator="\n")
        for line in full_text.splitlines():
            if f"{hour_str} •" in line or f"{hour_str}•" in line:
                log.debug(f"[parse_tier] Ligne candidate : {line!r}")
                # Cherche "(X tier)" dans la ligne
                match = re.search(r"\(([A-F])\s+tier\)", line, re.IGNORECASE)
                if match:
                    tier = match.group(1).upper()
                    log.info(f"[parse_tier] Tier trouvé : {tier}")
                    return tier
    except Exception as e:
        log.warning(f"[parse_tier] Erreur lors du parsing HTML : {e}")

    log.warning("[parse_tier] Tier non trouvé, fallback → Inconnu")
    return "Inconnu"


def extract_node_info(worldstate: dict, node_id: str) -> dict | None:
    """
    Extrait les infos du nœud (planète, nom, type de mission, faction)
    depuis le worldstate Warframe.
    """
    sol_nodes = worldstate.get("solNodes", {})

    # solNodes peut être un dict {tag: {...}} ou une liste selon la version de l'API
    if isinstance(sol_nodes, dict):
        node = sol_nodes.get(node_id)
        if node:
            return {
                "planet":       node.get("planet", "Inconnu"),
                "node_name":    node.get("value", node_id).split(" (")[0],
                "mission_type": node.get("type", "Inconnu"),
                "faction":      node.get("enemy", "Inconnu"),
            }
    elif isinstance(sol_nodes, list):
        for node in sol_nodes:
            if node.get("tag") == node_id or node.get("id") == node_id:
                return {
                    "planet":       node.get("planet", "Inconnu"),
                    "node_name":    node.get("name", node_id).split(" (")[0],
                    "mission_type": node.get("missionType", "Inconnu"),
                    "faction":      node.get("enemy", "Inconnu"),
                }

    log.warning(f"[extract_node] Node {node_id!r} introuvable dans le worldstate.")
    return None


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

        # 3. Récupère le tier depuis la page HTML browse.wf/arbys
        html = await fetch_text(session, ARBYS_HTML_URL)
        if html:
            result["tier"] = parse_tier_from_html(html)
        else:
            log.error("Impossible de récupérer la page HTML pour le tier.")

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
        """Récupère l'Arbitration et envoie l'embed dans le channel configuré."""
        channel_id = self.config.get("channel_id")
        if not channel_id:
            log.warning("[notify] Aucun channel configuré. Utilisez /setchannel d'abord.")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            log.error(f"[notify] Channel {channel_id} introuvable (bot absent du serveur ?).")
            return

        log.info(f"[notify] Envoi ({reason}) dans #{channel.name} ({channel_id})")
        try:
            data  = await get_current_arbitration()
            embed = build_embed(data)
            await channel.send(embed=embed)
            log.info(f"[notify] Embed envoyé avec succès : {data}")
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
