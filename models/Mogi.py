import discord
from .Config import LeaderboardConfig
from datetime import datetime
            
class Player:
    def __init__ (self, member:discord.Member, lounge_name: str, mmr: int):
        self.member = member
        self.lounge_name = lounge_name
        self.mmr = mmr
        self.confirmed = False
        self.score = 0

class Team:
    def __init__ (self, players: list[Player]):
        self.players = players
        self.avg_mmr = sum([p.mmr for p in self.players]) / len(self.players)

    def recalc_avg(self):
        self.avg_mmr = sum([p.mmr for p in self.players]) / len(self.players)

    def is_registered(self):
        for player in self.players:
            if player.confirmed is False:
                return False
        return True

    def has_player(self, member: discord.Member):
        for player in self.players:
            if player.member.id == member.id:
                return True
        return False

    def get_player(self, member: discord.Member):
        for player in self.players:
            if player.member.id == member.id:
                return player
        return None

    def sub_player(self, sub_out: Player, sub_in: Player):
        for i, player in enumerate(self.players):
            if player == sub_out:
                self.players[i] = sub_in
                self.recalc_avg()
                return   

    def num_confirmed(self):
        count = 0
        for player in self.players:
            if player.confirmed:
                count += 1
        return count

    def get_unconfirmed(self):
        unconfirmed: list[Player] = []
        for player in self.players:
            if not player.confirmed:
                unconfirmed.append(player)
        return unconfirmed

    def __lt__(self, other):
        if self.avg_mmr < other.avg_mmr:
            return True
        if self.avg_mmr > other.avg_mmr:
            return False

    def __gt__(self, other):
        return other.__lt__(self)

    def __str__(self):
        return ", ".join([p.lounge_name for p in self.players])

class Room:
    def __init__(self, teams: list[Team], room_num:int, thread:discord.Thread):
        self.teams = teams
        self.room_num = room_num
        self.thread = thread
        self.finished = False

    def get_player(self, member: discord.Member):
        for team in self.teams:
            player = team.get_player(member)
            if player:
                return player
            
class Mogi:
    def __init__ (self, sq_id:int, size:int, room_size: int, mogi_channel:discord.TextChannel, leaderboard: LeaderboardConfig,
                  is_automated = False, start_time: datetime | None = None, discord_event: discord.ScheduledEvent | None = None):
        self.started = False
        self.gathering = False
        self.making_rooms_run = False
        self.sq_id = sq_id
        self.room_size = room_size
        self.size = size
        self.mogi_channel = mogi_channel
        self.leaderboard = leaderboard
        self.teams: list[Team] = []
        self.rooms: list[Room] = []
        self.is_automated = is_automated
        self.discord_event = discord_event
        if not is_automated:
            self.start_time = None
        else:
            self.start_time = start_time

    def check_player(self, member:discord.Member):
        for team in self.teams:
            if team.has_player(member):
                return team
        return None

    def count_registered(self):
        count = 0
        for team in self.teams:
            if team.is_registered():
                count += 1
        return count

    def confirmed_list(self):
        confirmed: list[Team] = []
        for team in self.teams:
            if team.is_registered():
                confirmed.append(team)
        return confirmed

    def remove_id(self, squad_id:int):
        confirmed = self.confirmed_list()
        if squad_id < 1 or squad_id > len(confirmed):
            return None
        squad = confirmed[squad_id-1]
        self.teams.remove(squad)
        return squad

    def is_room_thread(self, channel_id:int):
        for room in self.rooms:
            if room.thread.id == channel_id:
                return True
        return False

    def get_room_from_thread(self, channel_id:int):
        for room in self.rooms:
            if room.thread.id == channel_id:
                return room
        return None