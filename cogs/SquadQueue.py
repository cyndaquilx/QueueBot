import discord
from discord.ext import commands, tasks
from discord import app_commands
from dateutil.parser import parse
from datetime import datetime, timedelta
import time
import json
from models.Mogi import Mogi, Team, Room, Player
from models.Config import LeaderboardConfig
from models import SquadQueueBot
from util import get_server_config, leaderboard_autocomplete, get_leaderboard_slash, format_autocomplete, get_mmr, room_size_autocomplete

class SquadQueue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # keys are discord.Guild objects, values are list of Mogi instances
        self.scheduled_events: dict[discord.Guild, list[Mogi]] = {}
        # keys are discord.TextChannel objects, values are instances of Mogi
        self.ongoing_events: dict[discord.TextChannel, Mogi] = {}
        
        self._scheduler_task = self.sqscheduler.start()
        self._msgqueue_task = self.send_queued_messages.start()
        self._list_task = self.list_task.start()

        self.msg_queue: dict[discord.TextChannel, list[str]] = {}
        self.list_messages = {}

        with open('./timezones.json', 'r') as cjson:
            self.timezones = json.load(cjson)

    async def lockdown(self, channel:discord.TextChannel):
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
        await channel.send("Locked down " + channel.mention)

    async def unlockdown(self, channel:discord.TextChannel):
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
        await channel.send("Unlocked " + channel.mention)

    #either adds a message to the message queue or sends it, depending on
    #server settings
    async def queue_or_send(self, ctx: commands.Context, leaderboard: LeaderboardConfig, msg:str, delay=0):
        assert isinstance(ctx.channel, discord.TextChannel)
        if leaderboard.queue_messages:
            if ctx.channel not in self.msg_queue.keys():
                self.msg_queue[ctx.channel] = []
            self.msg_queue[ctx.channel].append(msg)
        else:
            sendmsg = await ctx.send(msg)
            if delay > 0:
                await sendmsg.delete(delay=delay)

    #goes thru the msg queue for each channel and combines them
    #into as few messsages as possible, then sends them
    @tasks.loop(seconds=2)
    async def send_queued_messages(self):
        try:
            for channel in self.msg_queue.keys():
                channel_queue = self.msg_queue[channel]
                sentmsgs = []
                msg = ""
                for i in range(len(channel_queue)-1, -1, -1):
                    msg = channel_queue.pop(i) + "\n" + msg
                    if len(msg) > 1500:
                        sentmsgs.append(msg)
                        msg = ""
                if len(msg) > 0:
                    sentmsgs.append(msg)
                for i in range(len(sentmsgs)-1, -1, -1):
                    await channel.send(sentmsgs[i])
        except Exception as e:
            print(e)

    def get_mogi(self, ctx: commands.Context):
        assert isinstance(ctx.channel, discord.TextChannel)
        if ctx.channel in self.ongoing_events:
            return self.ongoing_events[ctx.channel]
        return None

    async def is_started(self, ctx: commands.Context, mogi: Mogi):
        if not mogi.started:
            await ctx.send("Mogi has not been started yet... type !start")
            return False
        return True

    async def is_gathering(self, ctx: commands.Context, mogi: Mogi):
        if not mogi.gathering:
            await ctx.send("Mogi is closed; players cannot join or drop from the event")
            return False
        return True

    @commands.command(aliases=['c'])
    @commands.max_concurrency(number=1, wait=True)
    @commands.guild_only()
    async def can(self, ctx: commands.Context, members:commands.Greedy[discord.Member]):
        """Tag your partners to invite them to a mogi or accept a invitation to join a mogi"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return

        assert isinstance(ctx.author, discord.Member)
        # logic when player is already in squad
        player_team = mogi.check_player(ctx.author)
        if player_team is not None:
            p = player_team.get_player(ctx.author)
            assert p is not None
            # if player is in a squad already but tries to make a new one
            if len(members) > 0:
                msg = f"{p.lounge_name} is already in a squad for this event `("
                msg += ", ".join([pl.lounge_name for pl in player_team.players])
                msg += f")`, so you cannot create a new squad. Please type `!d` if this is an error."
                await self.queue_or_send(ctx, mogi.leaderboard, msg)
                return
            # if player already said !c, give error msg
            if p.confirmed:
                await self.queue_or_send(ctx, mogi.leaderboard, f"{p.lounge_name} has already confirmed for this event; type `!d` to drop")
                return
            p.confirmed = True
            confirm_count = player_team.num_confirmed()
            msg = f"{p.lounge_name} has confirmed for their squad [{confirm_count}/{mogi.size}]\n"
            # if squad isn't full
            if confirm_count != mogi.size:
                msg += "Missing players: "
                msg += ", ".join([pl.lounge_name for pl in player_team.get_unconfirmed()])
            # if squad is full
            else:
                msg += f"`Squad successfully added to mogi list [{mogi.count_registered()} teams]`:\n"
                for i, pl in enumerate(player_team.players):
                    msg += f"`{i+1}.` {pl.member.mention} {pl.lounge_name} ({pl.mmr} MMR)\n"
            await self.queue_or_send(ctx, mogi.leaderboard, msg)
            await self.check_room_channels(mogi)
            await self.check_num_teams(mogi)
            return

        # logic when player is not already in a squad
        
        if len(members) != (mogi.size - 1):
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.display_name} didn't tag the correct number of people for this format ({mogi.size-1}), please try again")
            return
        # input validation for pinged members
        if len(members) != len(set(members)):
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.mention}, duplicate players are not allowed for a squad, please try again")
            return
        for member in members:
            player_team = mogi.check_player(member)
            if player_team is not None:
                p = player_team.get_player(member)
                assert p is not None
                msg = f"{p.lounge_name} is already in a squad for this event `("
                msg += ", ".join([pl.lounge_name for pl in player_team.players])
                msg += ")` They should type `!d` if this is an error."
                await self.queue_or_send(ctx, mogi.leaderboard, msg)
                return
            if member == ctx.author:
                await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.mention}, duplicate players are not allowed for a squad, please try again")
                return
        # checking players' mmr
        check_players = [ctx.author]
        check_players.extend(members)
        players = await get_mmr(mogi.leaderboard, check_players)
        not_found = []
        found_players: list[Player] = []
        for i, player in enumerate(players):
            if player is None:
                not_found.append(check_players[i].display_name)
            else:
                found_players.append(player)
        if len(not_found) > 0:
            msg = f"{ctx.author.mention} MMR for the following players could not be found: "
            msg += ", ".join(not_found)
            msg += ". Please contact a staff member for help"
            await self.queue_or_send(ctx, mogi.leaderboard, msg)
            return
        found_players[0].confirmed = True
        squad = Team(found_players)
        mogi.teams.append(squad)
        if len(players) > 1:
            msg = f"{found_players[0].lounge_name} has created a squad with "
            msg += ", ".join([p.lounge_name for p in found_players[1:]])
            msg += f"; each player must type `!c` to join the queue [1/{mogi.size}]\n"
            await self.queue_or_send(ctx, mogi.leaderboard, msg)
        else:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{found_players[0].lounge_name} has joined the mogi `[{mogi.count_registered()} players]`")
            await self.check_room_channels(mogi)
            await self.check_num_teams(mogi)

    @commands.command(aliases=['d'])
    @commands.max_concurrency(number=1,wait=True)
    @commands.guild_only()
    async def drop(self, ctx: commands.Context):
        """Remove your squad from a mogi"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        assert isinstance(ctx.author, discord.Member)
        squad = mogi.check_player(ctx.author)
        if squad is None:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.display_name} is not currently in a squad for this event; type `!c @partnerNames`")
            return
        mogi.teams.remove(squad)
        msg = "Removed team "
        msg += ", ".join([p.lounge_name for p in squad.players])
        if len(squad.get_unconfirmed()) == 0:
            msg += " from mogi list"
        else:
            msg += " from unfilled squads"
        await self.queue_or_send(ctx, mogi.leaderboard, msg, delay=5)

    @commands.command()
    @commands.max_concurrency(number=1, wait=True)
    @commands.guild_only()
    async def sub(self, ctx: commands.Context, sub_out:discord.Member, sub_in:discord.Member):
        """Replace a player on your team"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        if mogi.size == 1:
            await self.queue_or_send(ctx, mogi.leaderboard, "You cannot use the `!sub` command in FFA!")
            return
        assert isinstance(ctx.author, discord.Member)
        squad = mogi.check_player(ctx.author)
        if squad is None:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.display_name} is not currently in a squad for this event; type `!c @partnerNames`")
            return
        if ctx.author.id == sub_out.id:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.mention}, you cannot sub yourself out")
            return
        sub_out_player = squad.get_player(sub_out)
        if not sub_out_player:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{sub_out.display_name} is not in the squad `{str(squad)}`, so they can't be subbed out")
            return
        in_squad = mogi.check_player(sub_in) 
        if in_squad is not None:
            in_squad_player = in_squad.get_player(sub_in)
            assert in_squad_player is not None
            await self.queue_or_send(ctx, mogi.leaderboard, f"{in_squad_player.lounge_name} is already in a squad for this event `{str(in_squad)}`, they should type `!d` if this is an error.")
            return
        sub_in_player = await get_mmr(mogi.leaderboard, [sub_in])
        if sub_in_player[0] is None:
            await self.queue_or_send(ctx, mogi.leaderboard, f"MMR for player {sub_in.display_name} could not be found! Please contact a staff member for help")
            return
        squad.sub_player(sub_out_player, sub_in_player[0])
        await self.queue_or_send(ctx, mogi.leaderboard, f"{sub_out_player.lounge_name} has been replaced with {sub_in_player[0].lounge_name} in the squad `{str(squad)}`; they must type `!c` to confirm")
        
    @commands.command(aliases=['r'])
    @commands.max_concurrency(number=1,wait=True)
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, member: discord.Member):
        """Removes the mentioned player's squad from the mogi list"""
        if not await self.has_roles(ctx):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        squad = mogi.check_player(member)
        if not squad:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.mention} this member could not be found in the mogi")
            return
        mogi.teams.remove(squad)
        await self.queue_or_send(ctx, mogi.leaderboard, f"Removed squad {str(squad)} from mogi list")

    @commands.command()
    @commands.cooldown(1, 30, commands.BucketType.member)
    @commands.guild_only()
    async def squad(self, ctx: commands.Context):
        """Displays information about your squad for a mogi"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        assert isinstance(ctx.author, discord.Member)
        squad = mogi.check_player(ctx.author)
        if squad is None:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.display_name} is not currently in a squad for this event; type `!c @partnerNames`")
            return
        confirm_count = squad.num_confirmed()
        msg = f"`{ctx.author.display_name}'s squad "
        if confirm_count == mogi.size:
            msg += f"[registered]`\n"
        else:
            msg += f"[{confirm_count}/{mogi.size}] confirmed`\n"
        for i, player in enumerate(squad.players):
            msg += f"`{i+1}.` {player.lounge_name} ({player.mmr} MMR) "
            if player.confirmed:
                msg += "`✓ Confirmed`\n"
            else:
                msg += "`✘ Unconfirmed`\n"
        await self.queue_or_send(ctx, mogi.leaderboard, msg, delay=30)

    def get_list_messages(self, mogi: Mogi):
        mogi_list = mogi.confirmed_list()
        sorted_mogi_list = sorted(mogi_list, reverse=True)
        msg = f""
        for i in range(len(sorted_mogi_list)):
            msg += f"`{i+1}.` "
            msg += ", ".join([p.lounge_name for p in sorted_mogi_list[i].players])
            msg += f" ({sorted_mogi_list[i].avg_mmr:.1f} MMR)\n"
        players_per_mogi = mogi.room_size
        if(len(sorted_mogi_list) % (players_per_mogi/mogi.size) != 0):
            num_next = int(len(sorted_mogi_list) % (players_per_mogi/mogi.size))
            teams_per_room = int(players_per_mogi/mogi.size)
            num_rooms = int(len(sorted_mogi_list) / (players_per_mogi/mogi.size))+1
            msg += f"`[{num_next}/{teams_per_room}] teams for {num_rooms} rooms`"
        lines = msg.split("\n")
        messages = []
        curr_msg = f"`SQ #{mogi.sq_id} Mogi List`\n"
        for i, line in enumerate(lines):
            if len(curr_msg + line + "\n\n") > 2000:
                messages.append(curr_msg)
                curr_msg = ""
            curr_msg += f"{line}\n"
            if (i+1) % (players_per_mogi/mogi.size) == 0:
                curr_msg += "\n"
        if len(curr_msg) > 0:
            messages.append(curr_msg)
        return messages

    @commands.command(aliases=['l'])
    @commands.cooldown(1, 60)
    @commands.guild_only()
    async def list(self, ctx: commands.Context):
        """Display the list of confirmed squads for a mogi; sends 15 at a time to avoid
           reaching 2000 character limit"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if not await self.is_started(ctx, mogi):
            return
        #mogi_guild = mogi.mogi_channel.guild
        list_channel_id = mogi.leaderboard.list_channel
        list_channel = ctx.bot.get_channel(list_channel_id)
        if list_channel:
            await self.queue_or_send(ctx, mogi.leaderboard, f"{ctx.author.mention} {list_channel.jump_url}")
            return
        msgs = self.get_list_messages(mogi)
        for msg in msgs:
            await ctx.send(msg)
    
    @tasks.loop(seconds=60)
    async def list_task(self):
        if len(self.ongoing_events) == 0:
            return
        for mogi in self.ongoing_events.values():
            list_channel_id = mogi.leaderboard.list_channel
            list_channel = self.bot.get_channel(list_channel_id)
            if not list_channel:
                continue
            assert isinstance(list_channel, discord.TextChannel)
            if not mogi.gathering:
                await self.delete_list_messages(list_channel, 0)
                continue

            new_messages = self.get_list_messages(mogi)
            await self.delete_list_messages(list_channel, len(new_messages))

            list_messages = self.list_messages[list_channel]
            try:
                for i, message in enumerate(new_messages):
                    if i < len(list_messages):
                        old_message = list_messages[i]
                        await old_message.edit(content=message)
                    else:
                        new_message = await list_channel.send(message)
                        list_messages.append(new_message)
            except Exception as e:
               print(e, flush=True)
               await self.delete_list_messages(list_channel, 0)
            
    async def delete_list_messages(self, channel: discord.TextChannel, new_list_size: int):
        try:
            messages_to_delete = []
            if channel not in self.list_messages.keys():
                self.list_messages[channel] = []
            list_messages = self.list_messages[channel]
            while len(list_messages) > new_list_size:
                messages_to_delete.append(list_messages.pop())
            await channel.delete_messages(messages_to_delete)
        except Exception as e:
            print(e, flush=True)
        
    #check if user has roles defined in config.json
    async def has_roles(self, ctx: commands.Context):
        assert isinstance(ctx.author, discord.Member)
        server_config = get_server_config(ctx)
        check_roles = server_config.admin_roles + server_config.staff_roles
        for role_id in check_roles:
            if ctx.author.get_role(role_id):
                return True
        return False
        
    @commands.command()
    @commands.guild_only()
    async def close(self, ctx: commands.Context):
        """Close the mogi so players can't join or drop"""
        if not await self.has_roles(ctx):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        mogi.gathering = False
        mogi.is_automated = False
        assert isinstance(ctx.channel, discord.TextChannel)
        await self.lockdown(ctx.channel)
        await ctx.send("Mogi is now closed; players can no longer join or drop from the event")

    async def endMogi(self, mogi_channel):
        mogi = self.ongoing_events[mogi_channel]
        if mogi:
            del self.ongoing_events[mogi_channel]

    @commands.command()
    @commands.guild_only()
    async def end(self, ctx: commands.Context):
        if not await self.has_roles(ctx):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        await self.endMogi(mogi.mogi_channel)
        await ctx.send(f"{ctx.author.display_name} has ended the mogi")
        
    @commands.command()
    @commands.guild_only()
    async def open(self, ctx: commands.Context):
        """Close the mogi so players can't join or drop"""
        if not await self.has_roles(ctx):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if not await self.is_started(ctx, mogi):
            return
        if mogi.gathering:
            await(await ctx.send("Mogi is already open; players can join and drop from the event")
                  ).delete(delay=5)
            return
        mogi.gathering = True
        mogi.is_automated = False
        assert isinstance(ctx.channel, discord.TextChannel)
        await self.unlockdown(ctx.channel)
        await ctx.send("Mogi is now open; players can join and drop from the event")

    #command to add staff to room thread channels; users can't add new users to private threads,
    #so the bot has to with this command
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @commands.guild_only()
    async def staff(self, ctx: commands.Context):
        """Calls staff to the current channel. Only works in thread channels for SQ rooms."""
        assert ctx.guild is not None
        mogi = None
        for m in self.ongoing_events.values():
            if m.is_room_thread(ctx.channel.id):
                mogi = m
                break
        if mogi is None:
            return
        server_config = get_server_config(ctx)
        lounge_staff_roles = server_config.staff_roles
        mentions = " ".join([role.mention for role_id in lounge_staff_roles if (role := ctx.guild.get_role(role_id)) is not None])
        await ctx.send(mentions)

    @commands.command()
    async def scoreboard(self, ctx: commands.Context):
        """Displays the scoreboard of the room. Only works in thread channels for SQ rooms."""
        room = None
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                room = mogi.get_room_from_thread(ctx.channel.id)
                break
        if not room:
            return
        msg = f"`!submit {mogi.size} sq #RESULTS\n"
        for i, team in enumerate(room.teams):
            msg += f"Team {i+1} - {chr(ord('A')+i)}\n"
            for player in team.players:
                msg += f"{player.lounge_name} [] {player.score}\n"
            msg += "\n"
        msg += f"`Fill out the scores for each player and then use the `!submit` command to submit the table."
        await ctx.send(msg)

    # make thread channels while the event is gathering instead of at the end,
    # since discord only allows 50 thread channels to be created per 5 minutes.
    async def check_room_channels(self, mogi: Mogi):
        num_teams = mogi.count_registered()
        players_per_mogi = mogi.room_size
        num_rooms = int(num_teams / (players_per_mogi/mogi.size))
        num_created_rooms = len(mogi.rooms)
        if num_created_rooms >= num_rooms:
            return
        for i in range(num_created_rooms, num_rooms):
            room_name = f"SQ{mogi.sq_id} Room {i+1}"
            try: 
                room_channel = await mogi.mogi_channel.create_thread(name=room_name,
                                                                    auto_archive_duration=60,
                                                                    invitable=False)
                await room_channel.send(room_name)
            except Exception as e:
                print(e)
                err_msg = f"\nAn error has occurred while creating a room channel:\n{e}"
                await mogi.mogi_channel.send(err_msg)
                return
            mogi.rooms.append(Room([], i+1, room_channel))
    
    # add teams to the room threads that we have already created
    async def add_teams_to_rooms(self, mogi: Mogi, open_time:int, started_automatically=False):
        if open_time >= 60 or open_time < 0:
            await mogi.mogi_channel.send("Please specify a valid time (in minutes) for rooms to open (00-59)")
            return
        if mogi.making_rooms_run and started_automatically:
            return
        players_per_mogi = mogi.room_size
        num_rooms = int(mogi.count_registered() / (players_per_mogi/mogi.size))
        if num_rooms == 0:
            await mogi.mogi_channel.send(f"Not enough players to fill a room! Try this command with at least {int(players_per_mogi/mogi.size)} teams")
            return
        await self.lockdown(mogi.mogi_channel)
        mogi.making_rooms_run = True
        if mogi.gathering:
            mogi.gathering = False
            await mogi.mogi_channel.send("Mogi is now closed; players can no longer join or drop from the event")
        
        pen_time = open_time + 6
        start_time = open_time + 10
        while pen_time >= 60:
            pen_time -= 60
        while start_time >= 60:
            start_time -= 60
        players_per_mogi = mogi.room_size
        teams_per_room = int(players_per_mogi/mogi.size)
        num_teams = int(num_rooms * teams_per_room)
        final_list = mogi.confirmed_list()[0:num_teams]
        sorted_list = sorted(final_list, reverse=True)

        extra_members = []
        for m in mogi.leaderboard.pinged_member_ids:
            extra_members.append(mogi.mogi_channel.guild.get_member(m))

        rooms = mogi.rooms
        for i in range(num_rooms):
            room_name = f"SQ{mogi.sq_id} Room {i+1}"
            msg = f"`Room {i+1}`\n"
            scoreboard = f"Table: `!scoreboard`"
            mentions = ""
            start_index = int(i*teams_per_room)
            for j in range(teams_per_room):
                msg += f"`{j+1}.` "
                team = sorted_list[start_index+j]
                msg += ", ".join([p.lounge_name for p in team.players])
                msg += f" ({int(team.avg_mmr)} MMR)\n"
                mentions += " ".join([p.member.mention for p in team.players])
                mentions += " "
            room_msg = msg
            mentions += " ".join([m.mention for m in extra_members if m is not None])
            room_msg += f"{scoreboard}\n"
            room_msg += ("\nDecide a host amongst yourselves; room open at :%02d, penalty at :%02d, start by :%02d. Good luck!\n\n"
                        % (open_time, pen_time, start_time))
            room_msg += "\nIf you need staff's assistance, use the `!staff` command in this channel.\n"
            room_msg += mentions
            try:
                if i >= len(rooms):
                    room_channel = await mogi.mogi_channel.create_thread(name=room_name,
                                                                    auto_archive_duration=60,
                                                                    invitable=False)
                    rooms.append(Room([], i+1, room_channel))
                curr_room = rooms[i]
                room_channel = curr_room.thread
                curr_room.teams = sorted_list[start_index:start_index+teams_per_room]
                await room_channel.send(room_msg)
            except Exception as e:
                print(e)
                err_msg = f"\nAn error has occurred while creating the room channel; please contact your opponents in DM or another channel\n"
                err_msg += mentions
                msg += err_msg
                room_channel = None
            await mogi.mogi_channel.send(msg)
        if num_teams < mogi.count_registered():
            missed_teams = mogi.confirmed_list()[num_teams:mogi.count_registered()]
            msg = "`Late teams:`\n"
            for i in range(len(missed_teams)):
                msg += f"`{i+1}.` "
                msg += ", ".join([p.lounge_name for p in missed_teams[i].players])
                msg += f" ({int(missed_teams[i].avg_mmr)} MMR)\n"
            await mogi.mogi_channel.send(msg)
        mogi_guild = mogi.mogi_channel.guild
        list_channel_id = mogi.leaderboard.list_channel
        list_channel = self.bot.get_channel(list_channel_id)
        if not list_channel:
            return
        assert isinstance(list_channel, discord.TextChannel)
        for i in range(num_rooms):
            room = rooms[i]
            msg = f"`SQ #{mogi.sq_id} Room {i+1} -` {room.thread.jump_url}\n"
            for i, team in enumerate(room.teams):
                msg += f"`{i+1}.` {', '.join([p.lounge_name for p in team.players])} ({int(team.avg_mmr)} MMR)\n"
            await list_channel.send(msg)

    @commands.command()
    @commands.guild_only()
    @commands.max_concurrency(number=1, wait=False)
    async def makeRooms(self, ctx, openTime:int):
        """Makes thread channels for SQ rooms."""
        if not await self.has_roles(ctx):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)):
            return
        await self.add_teams_to_rooms(mogi, openTime)
        
    async def scheduler_mogi_start(self):
        cur_time = datetime.now()
        for guild in self.scheduled_events.values():
            to_remove = [] #Keep a list of indexes to remove - can't remove while iterating
            for i, mogi in enumerate(guild):
                ts = mogi.leaderboard.time_settings
                assert mogi.start_time is not None
                if(mogi.start_time - timedelta(minutes=ts.queue_open_time)) < cur_time:
                    if mogi.mogi_channel in self.ongoing_events.keys() and self.ongoing_events[mogi.mogi_channel].gathering:
                        to_remove.append(i)
                        await mogi.mogi_channel.send(f"Because there is an ongoing event right now, the following event has been removed:\n{self.get_event_str(mogi)}\n")
                    else:
                        if mogi.mogi_channel in self.ongoing_events.keys():
                            if self.ongoing_events[mogi.mogi_channel].started:
                                await self.endMogi(mogi.mogi_channel)
                        to_remove.append(i)
                        self.ongoing_events[mogi.mogi_channel] = mogi
                        mogi.started = True
                        mogi.gathering = True
                        await self.unlockdown(mogi.mogi_channel)
                        await mogi.mogi_channel.send(f"A {mogi.size}v{mogi.size} mogi has been started - @here Type `!c`, `!d`, or `!list`")
            for ind in reversed(to_remove):
                del guild[ind]

    async def check_num_teams(self, mogi: Mogi):
        if not mogi.gathering or not mogi.is_automated:
            return
        cur_time = datetime.now()
        ts = mogi.leaderboard.time_settings
        queue_open_time = timedelta(minutes=ts.queue_open_time)
        joining_time = timedelta(minutes=ts.joining_time)
        assert mogi.start_time is not None
        if mogi.start_time - queue_open_time + joining_time <= cur_time:
            players_per_mogi = mogi.room_size
            numLeftoverTeams = mogi.count_registered() % int((players_per_mogi/mogi.size))
            if numLeftoverTeams == 0:
                mogi.gathering = False
                await self.lockdown(mogi.mogi_channel)
                await mogi.mogi_channel.send("A sufficient number of teams has been reached, so the mogi has been closed to extra teams. Rooms will be made within the next minute.")

    async def ongoing_mogi_checks(self):
        for mogi in self.ongoing_events.values():
            #If it's not automated, not started, we've already started making the rooms, don't run this
            if not mogi.is_automated or not mogi.started or mogi.making_rooms_run:
                continue
            assert mogi.start_time is not None
            cur_time = datetime.now()
            ts = mogi.leaderboard.time_settings
            queue_open_time = timedelta(minutes=ts.queue_open_time)
            joining_time = timedelta(minutes=ts.joining_time)
            extension_time = timedelta(minutes=ts.extension_time)
            if (mogi.start_time - queue_open_time + joining_time + extension_time) <= cur_time:
                await self.add_teams_to_rooms(mogi, (mogi.start_time.minute)%60, True)
                continue
            if mogi.start_time - queue_open_time + joining_time <= cur_time:
                #check if there are an even amount of teams since we are past the queue time
                players_per_mogi = mogi.room_size
                numLeftoverTeams = mogi.count_registered() % int((players_per_mogi/mogi.size))
                if numLeftoverTeams == 0:
                    await self.add_teams_to_rooms(mogi, (mogi.start_time.minute)%60, True)
                    continue
                else:
                    if int(cur_time.second / 20) == 0:
                        force_time = mogi.start_time - queue_open_time + joining_time + extension_time
                        minutes_left = int((force_time - cur_time).seconds/60)
                        x_teams = int(int(players_per_mogi/mogi.size) - numLeftoverTeams)
                        await mogi.mogi_channel.send(f"Need {x_teams} more team(s) to start immediately. Starting in {minutes_left} minute(s) regardless.")

    @tasks.loop(seconds=20.0)
    async def sqscheduler(self):
        """Scheduler that checks if it should start mogis and close them"""
        #It may seem silly to do try/except Exception, but this coroutine **cannot** fail
        #This coroutine *silently* fails and stops if exceptions aren't caught - an annoying abtraction of asyncio
        #This is unacceptable considering people are relying on these mogis to run, so we will not allow this routine to stop
        try:
            await self.scheduler_mogi_start()
        except Exception as e:
            print(e)
        try:
            await self.ongoing_mogi_checks()
        except Exception as e:
            print(e)

    def getTime(self, schedule_time:str, timezone:str):
        """Returns a DateTime object representing the UTC equivalent of the given time."""
        if schedule_time.isnumeric():
            schedule_time += ":00"
        utc_offset = time.altzone if time.localtime().tm_isdst > 0 else time.timezone
        time_adjustment = timedelta(seconds=utc_offset)
        timezone_adjustment = timedelta(hours=0)
        if timezone.upper() in self.timezones.keys():
            timezone_adjustment = timedelta(hours=self.timezones[timezone.upper()])
        try:
            actual_time = parse(schedule_time)
        except Exception as e:
            return None
        corrected_time = actual_time - time_adjustment - timezone_adjustment
        return corrected_time

    @app_commands.command(name="get_time_discord")
    async def get_time_command(self, interaction:discord.Interaction,
                    schedule_time:str, timezone:str):
        """Get the Discord timestamp string for a time"""
        actual_time = self.getTime(schedule_time, timezone)
        if actual_time:
            event_str = discord.utils.format_dt(actual_time, style="F")
        else:
            event_str = "none"
        await interaction.response.send_message(f"`{event_str}`", ephemeral=True)

    @app_commands.command(name="schedule_event")
    @app_commands.autocomplete(size=format_autocomplete)
    @app_commands.autocomplete(leaderboard=leaderboard_autocomplete)
    @app_commands.autocomplete(room_size=room_size_autocomplete)
    @app_commands.guild_only()
    async def schedule_event(self, interaction:discord.Interaction[SquadQueueBot],
                       sq_id: int, room_size: int, size:int,
                       schedule_time:str, timezone:str, leaderboard: str | None):
        """Schedules an SQ event in the given channel at the given time."""
        assert interaction.guild is not None
        ctx = await commands.Context.from_interaction(interaction)
        lb = get_leaderboard_slash(ctx, leaderboard)
        if not await self.has_roles(ctx):
            await interaction.response.send_message("You do not have permissions to use this command",ephemeral=True)
            return
        actual_time = self.getTime(schedule_time, timezone)
        if actual_time is None:
            await interaction.response.send_message(f"I couldn't understand your time, so I couldn't schedule the event.",
            ephemeral=True)
            return
        if actual_time < datetime.now():
            bad_time = discord.utils.format_dt(actual_time, style="F")
            await interaction.response.send_message(f"That time is in the past! ({bad_time})"
            "Make sure your timezone is correct (with daylight savings taken into account, "
            "ex. EDT instead of EST if it's summer), and that you've entered the date if it's not today")
            return
        
        queue_open_time = timedelta(minutes=lb.time_settings.queue_open_time)
        joining_time = timedelta(minutes=lb.time_settings.joining_time)
        event_start_time = actual_time.astimezone() - queue_open_time
        event_end_time = event_start_time + joining_time
        if event_end_time < discord.utils.utcnow():
            bad_time = discord.utils.format_dt(event_end_time, style="F")
            await interaction.response.send_message("The queue for this event would end in the past! "
            f"({bad_time}) "
            "Make sure your timezone is correct (with daylight savings taken into account, "
            "ex. EDT instead of EST if it's summer), and that you've entered the date if it's not today")
            return
        size_name = f"{size}v{size}" if size > 1 else "FFA"
        if room_size not in lb.valid_room_sizes:
            await interaction.response.send_message(f"Invalid room size. Valid room sizes for this server are: {lb.valid_room_sizes}")
            return
        if size not in lb.valid_formats:
            await interaction.response.send_message(f"Invalid format. Valid formats for this server are: {lb.valid_formats}")
            return
        if room_size % size != 0:
            await interaction.response.send_message(f"The entered format ({size_name}) is not divisible by the specified room size ({room_size}).")
            return
        channel = ctx.bot.get_channel(lb.join_channel)

        await interaction.response.defer(thinking=True)

        if event_start_time < discord.utils.utcnow():
            #have to add 1 minute here, because utcnow() will technically be the past when the API request is sent
            event_start_time = discord.utils.utcnow() + timedelta(minutes=1)
        discord_event = await interaction.guild.create_scheduled_event(name=f"SQ #{sq_id}: {room_size}p {size_name} gathering players",
                                                       start_time = event_start_time,
                                                       end_time = event_end_time,
                                                       privacy_level = discord.PrivacyLevel.guild_only,
                                                       entity_type = discord.EntityType.external,
                                                       location=channel.mention)
        mogi = Mogi(sq_id, size, room_size, channel, lb, is_automated=True, start_time=actual_time, discord_event=discord_event)
        if interaction.guild not in self.scheduled_events.keys():
            self.scheduled_events[interaction.guild] = []
        self.scheduled_events[interaction.guild].append(mogi)
        event_str = self.get_event_str(mogi)
        #await interaction.response.send_message(f"Scheduled the following event:\n{event_str}")
        await interaction.followup.send(f"Scheduled the following event:\n{event_str}")

    def get_event_str(self, mogi: Mogi):
        assert mogi.start_time is not None
        mogi_time = discord.utils.format_dt(mogi.start_time, style="F")
        mogi_time_relative = discord.utils.format_dt(mogi.start_time, style="R")
        return(f"`#{mogi.sq_id}` **{mogi.room_size}p {mogi.size}v{mogi.size}:** {mogi_time} - {mogi_time_relative}")

    @app_commands.command(name="remove_event")
    @app_commands.guild_only()
    async def remove_event(self, interaction:discord.Interaction, event_id:int):
        """Removes an event from the schedule"""
        assert interaction.guild is not None
        ctx = await commands.Context.from_interaction(interaction)
        if not await self.has_roles(ctx):
            await interaction.response.send_message("You do not have permissions to use this command",ephemeral=True)
            return
        if interaction.guild not in self.scheduled_events.keys():
            await interaction.response.send_message("This event number isn't in the schedule. Do `!view_schedule` to see the scheduled events.",
                                                    ephemeral=True)
            return
        for event in self.scheduled_events[interaction.guild]:
            if event.sq_id == event_id:
                self.scheduled_events[interaction.guild].remove(event)
                if event.discord_event:
                    await event.discord_event.cancel()
                await interaction.response.send_message(f"Removed the following event:\n{self.get_event_str(event)}")
                return
        await interaction.response.send_message("This event number isn't in the schedule. Do `!view_schedule` to see the scheduled events.")

    @commands.command()
    @commands.guild_only()
    async def view_schedule(self, ctx: commands.Context, copy_paste=""):
        """View the SQ schedule. Use !view_schedule cp to get a copy/pastable version"""
        assert ctx.guild is not None
        server_events = self.scheduled_events.get(ctx.guild, None)
        if server_events is None:
            await ctx.send("There are no SQ events scheduled in this server yet. Use /schedule_event to schedule one.")
            return
        server_schedule = sorted(server_events, key = lambda event: event.sq_id)
        if len(server_schedule) == 0:
            await ctx.send("There are no SQ events scheduled in this server yet. Use /schedule_event to schedule one.")
            return
        msg = ""
        if copy_paste == "cp":
            msg += "```"
        for event in server_schedule:
            msg += f"{self.get_event_str(event)}\n"
        if copy_paste == "cp":
            msg += "```"
        await ctx.send(msg)
        return

    @commands.command()
    @commands.guild_only()
    async def view_timestamps(self, ctx: commands.Context, no4or6=False):
        assert ctx.guild is not None
        if ctx.guild not in self.scheduled_events.keys():
            await ctx.send("There are no SQ events scheduled in this server yet. Use /schedule_event to schedule one.")
            return
        server_schedule = sorted(self.scheduled_events[ctx.guild], key = lambda event: event.sq_id)
        if len(server_schedule) == 0:
            await ctx.send("There are no SQ events scheduled in this server yet. Use /schedule_event to schedule one.")
            return
        msg = ""
        for event in server_schedule:
            if no4or6 and event.size in [4, 6]:
                continue
            if event.start_time is None:
                continue
            msg += f"{int(event.start_time.timestamp())}\n"
        await ctx.send(msg)

    @commands.command(aliases=['pt'])
    async def parsetime(self, ctx: commands.Context, *, schedule_time:str):
        try:
            actual_time = parse(schedule_time)
            await ctx.send("```<t:" + str(int(time.mktime(actual_time.timetuple()))) + ":F>```")
        except (ValueError, OverflowError):
            await ctx.send("I couldn't figure out the date and time for your event. Try making it a bit more clear for me.")

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync(self, ctx: commands.Context):
        await self.bot.tree.sync()
        await ctx.send("synced")

    @commands.command(name="sync_server")
    @commands.is_owner()
    async def sync_server(self, ctx: commands.Context):
        await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send("synced")

    #@commands.command()
    #@commands.is_owner()
    async def reload(self, ctx: commands.Context):
        await ctx.bot.reload_extension("cogs.SquadQueue")
        await ctx.send("Done")
        
    @commands.command()
    @commands.is_owner()
    @commands.guild_only()
    async def add100(self, ctx: commands.Context):
        assert isinstance(ctx.author, discord.Member)
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        # checking players' mmr
        check_players = [ctx.author]
        players = await get_mmr(mogi.leaderboard, check_players)
        if players[0] is None:
            return
        players[0].confirmed = True
        squad = Team([players[0]]*mogi.size)
        for i in range(100):
            mogi.teams.append(squad)
        await ctx.send(f"Added {ctx.author.display_name} 100 times")
        await self.check_room_channels(mogi)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not isinstance(message.author, discord.Member):
            return
        if message.author.bot or not (
            message.content.isdecimal() and 12 <= int(
                message.content) <= 180):
            return
        room = None
        
        for mogi in self.ongoing_events.values():
            room = mogi.get_room_from_thread(message.channel.id)
            if room:
                break
        if not room:
            return
        player = room.get_player(message.author)
        if player:
            player.score = int(message.content)

async def setup(bot: SquadQueueBot):
    await bot.add_cog(SquadQueue(bot))
