from util.Exceptions import GuildNotFoundException, LeaderboardNotFoundException
from models import ServerConfig, LeaderboardConfig
from discord.ext import commands
import discord
from discord import app_commands

def get_server_config(ctx: commands.Context) -> ServerConfig:
    server_info: ServerConfig = ctx.bot.config.servers.get(ctx.guild.id, None)
    if not server_info:
        raise GuildNotFoundException
    return server_info

def get_leaderboard_slash(ctx: commands.Context, lb: str | None) -> LeaderboardConfig:
    server_info = get_server_config(ctx)
    # if we don't provide a leaderboard argument and there's only 1 leaderboard in the server
    # we should just return that leaderboard
    if lb is None and len(server_info.leaderboards) == 1:
        leaderboard = next(iter(server_info.leaderboards.values()))
    else:
        leaderboard = server_info.leaderboards.get(lb, None)
    if not leaderboard:
        raise LeaderboardNotFoundException
    return leaderboard

async def leaderboard_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    server_info: ServerConfig | None = interaction.client.config.servers.get(interaction.guild_id, None)
    if not server_info:
        return []
    choices = [app_commands.Choice(name=lb, value=lb) for lb in server_info.leaderboards]
    return choices