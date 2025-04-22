from discord.ext import commands
from models.Config import BotConfig

class SquadQueueBot(commands.Bot):
    def __init__(self, config: BotConfig, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config