import aiohttp
import discord
from mogi_objects import Player

headers = {'Content-type': 'application/json'}

async def mk8dx_150cc_mmr(config, members):
    base_url = config["url"] + '/api/player?'
    players = []
    async with aiohttp.ClientSession(
        auth=aiohttp.BasicAuth(
            config["username"], config["password"])) as session:
        for member in members:
            request_text = f"discordId={member.id}"
            request_url = base_url + request_text
            async with session.get(request_url,headers=headers) as resp:
                if resp.status != 200:
                    players.append(None)
                    continue
                player_data = await resp.json()
                if 'mmr' not in player_data.keys():
                    players.append(None)
                    continue
                players.append(Player(member, player_data['name'], player_data['mmr']))
    return players

async def get_mmr(config, members):
    return await mk8dx_150cc_mmr(config, members)
