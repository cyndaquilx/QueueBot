import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.app_commands import Choice
from dateutil.parser import parse
from datetime import datetime, timedelta
import collections
import time
import json
from mmr import get_mmr, mk8dx_150cc_fc
from mogi_objects import Mogi, Team, Player, Room
import asyncio

#Scheduled_Event = collections.namedtuple('Scheduled_Event', 'size time started mogi_channel')

class SquadQueue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # keys are discord.Guild objects, values are list of Mogi instances
        self.scheduled_events = {}
        # keys are discord.TextChannel objects, values are instances of Mogi
        self.ongoing_events = {}
        
        self._scheduler_task = self.sqscheduler.start()
        self._msgqueue_task = self.send_queued_messages.start()

        self.msg_queue = {}

        #number of minutes before scheduled time that queue should open
        self.QUEUE_OPEN_TIME = timedelta(minutes=bot.config["QUEUE_OPEN_TIME"])

        #number of minutes after QUEUE_OPEN_TIME that teams can join the mogi
        self.JOINING_TIME = timedelta(minutes=bot.config["JOINING_TIME"])

        #number of minutes after JOINING_TIME for any potential extra teams to join
        self.EXTENSION_TIME = timedelta(minutes=bot.config["EXTENSION_TIME"])

        with open('./timezones.json', 'r') as cjson:
            self.timezones = json.load(cjson)

    async def lockdown(self, channel:discord.TextChannel):
        everyone_perms = channel.permissions_for(channel.guild.default_role)
        if not everyone_perms.send_messages:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
        await channel.send("Locked down " + channel.mention)

    async def unlockdown(self, channel:discord.TextChannel):
        everyone_perms = channel.permissions_for(channel.guild.default_role)
        if everyone_perms.send_messages:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
        await channel.send("Unlocked " + channel.mention)

    #either adds a message to the message queue or sends it, depending on
    #server settings
    async def queue_or_send(self, ctx, msg, delay=0):
        if ctx.bot.config["queue_messages"] is True:
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

    

    def get_mogi(self, ctx):
        if ctx.channel in self.ongoing_events.keys():
            return self.ongoing_events[ctx.channel]
        return None

    async def is_started(self, ctx, mogi):
        if not mogi.started:
            await ctx.send("Mogi has not been started yet... type !start")
            return False
        return True

    async def is_gathering(self, ctx, mogi):
        if not mogi.gathering:
            await ctx.send("Mogi is closed; players cannot join or drop from the event")
            return False
        return True

    @commands.command(aliases=['c'])
    @commands.max_concurrency(number=1, wait=True)
    @commands.guild_only()
    async def can(self, ctx, members:commands.Greedy[discord.Member]):
        """Tag your partners to invite them to a mogi or accept a invitation to join a mogi"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return

        # logic when player is already in squad
        player_team = mogi.check_player(ctx.author)
        if player_team is not None:
            p = player_team.get_player(ctx.author)
            # if player is in a squad already but tries to make a new one
            if len(members) > 0:
                msg = f"{p.lounge_name} is already in a squad for this event `("
                msg += ", ".join([pl.lounge_name for pl in player_team.players])
                msg += f")`, so you cannot create a new squad. Please type `!d` if this is an error."
                await self.queue_or_send(ctx, msg)
                return
            # if player already said !c, give error msg
            if p.confirmed:
                await self.queue_or_send(ctx, f"{p.lounge_name} has already confirmed for this event; type `!d` to drop")
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
                    msg += f"`{i+1}.` {pl.lounge_name} ({pl.mmr} MMR)\n"
            await self.queue_or_send(ctx, msg)
            #await self.ongoing_mogi_checks()
            await self.check_num_teams(mogi)
            return

        # logic when player is not already in a squad
        
        if len(members) != (mogi.size - 1):
            await self.queue_or_send(ctx, f"{ctx.author.display_name} didn't tag the correct number of people for this format ({mogi.size-1}), please try again")
            return
        # input validation for pinged members
        if len(members) != len(set(members)):
            await self.queue_or_send(ctx, f"{ctx.author.mention}, duplicate players are not allowed for a squad, please try again")
            return
        for member in members:
            player_team = mogi.check_player(member)
            if player_team is not None:
                p = player_team.get_player(member)
                msg = f"{p.lounge_name} is already in a squad for this event `("
                msg += ", ".join([pl.lounge_name for pl in player_team.players])
                msg += ")` They should type `!d` if this is an error."
                await self.queue_or_send(ctx, msg)
                return
            if member == ctx.author:
                await self.queue_or_send(ctx, f"{ctx.author.mention}, duplicate players are not allowed for a squad, please try again")
                return
        # checking players' mmr
        check_players = [ctx.author]
        check_players.extend(members)
        players = await get_mmr(ctx.bot.config, check_players)
        not_found = []
        for i, player in enumerate(players):
            if player is None:
                not_found.append(check_players[i].display_name)
        if len(not_found) > 0:
            msg = f"{ctx.author.mention} MMR for the following players could not be found: "
            msg += ", ".join(not_found)
            msg += ". Please contact a staff member for help"
            await self.queue_or_send(ctx, msg)
            return
        players[0].confirmed = True
        squad = Team(players)
        mogi.teams.append(squad)
        if len(players) > 1:
            msg = f"{players[0].lounge_name} has created a squad with "
            msg += ", ".join([p.lounge_name for p in players[1:]])
            msg += f"; each player must type `!c` to join the queue [1/{mogi.size}]\n"
            await self.queue_or_send(ctx, msg)
        else:
            await self.queue_or_send(ctx, f"{players[0].lounge_name} has joined the mogi `[{mogi.count_registered()} players]`")

    @commands.command(aliases=['d'])
    @commands.max_concurrency(number=1,wait=True)
    @commands.guild_only()
    async def drop(self, ctx):
        """Remove your squad from a mogi"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        squad = mogi.check_player(ctx.author)
        if squad is None:
            await self.queue_or_send(ctx, f"{ctx.author.display_name} is not currently in a squad for this event; type `!c @partnerNames`")
            return
        mogi.teams.remove(squad)
        msg = "Removed team "
        msg += ", ".join([p.lounge_name for p in squad.players])
        if len(squad.get_unconfirmed()) == 0:
            msg += " from mogi list"
        else:
            msg += " from unfilled squads"
        await self.queue_or_send(ctx, msg, delay=5)

    @commands.command()
    @commands.max_concurrency(number=1, wait=True)
    @commands.guild_only()
    async def sub(self, ctx, sub_out:discord.Member, sub_in:discord.Member):
        """Replace a player on your team"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        if mogi.size == 1:
            await self.queue_or_send(ctx, "You cannot use the `!sub` command in FFA!")
            return
        squad = mogi.check_player(ctx.author)
        if squad is None:
            await self.queue_or_send(ctx, f"{ctx.author.display_name} is not currently in a squad for this event; type `!c @partnerNames`")
            return
        if ctx.author.id == sub_out.id:
            await self.queue_or_send(ctx, f"{ctx.author.mention}, you cannot sub yourself out")
            return
        if not squad.has_player(sub_out):
            await self.queue_or_send(ctx, f"{sub_out.display_name} is not in the squad `{str(squad)}`, so they can't be subbed out")
            return
        sub_out_player = squad.get_player(sub_out)
        in_squad = mogi.check_player(sub_in) 
        if in_squad is not None:
            in_squad_player = in_squad.get_player(sub_in)
            await self.queue_or_send(ctx, f"{in_squad_player.lounge_name} is already in a squad for this event `{str(in_squad)}`, they should type `!d` if this is an error.")
            return
        sub_in_player = await get_mmr(ctx.bot.config, [sub_in])
        if sub_in_player[0] is None:
            await self.queue_or_send(ctx, f"MMR for player {sub_in.display_name} could not be found! Please contact a staff member for help")
            return
        squad.sub_player(sub_out_player, sub_in_player[0])
        await self.queue_or_send(ctx, f"{sub_out_player.lounge_name} has been replaced with {sub_in_player[0].lounge_name} in the squad `{str(squad)}`; they must type `!c` to confirm")
        
    @commands.command(aliases=['r'])
    @commands.max_concurrency(number=1,wait=True)
    @commands.guild_only()
    async def remove(self, ctx, num:int):
        """Removes the given squad ID from the mogi list"""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        squad = mogi.remove_id(num)
        if squad is None:
            await self.queue_or_send(ctx, f"Invalid squad ID; there are {mogi.count_registered()} squads in the event")
            return
        await self.queue_or_send(ctx, f"Removed squad {str(squad)} from mogi list")

    @commands.command()
    @commands.cooldown(1, 30, commands.BucketType.member)
    @commands.guild_only()
    async def squad(self, ctx):
        """Displays information about your squad for a mogi"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        squad = mogi.check_player(ctx.author)
        if squad is None:
            await self.queue_or_send(ctx, f"{ctx.author.display_name} is not currently in a squad for this event; type `!c @partnerNames`")
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
        await self.queue_or_send(ctx, msg, delay=30)

    @commands.command(aliases=['l'])
    @commands.cooldown(1, 120)
    @commands.guild_only()
    async def list(self, ctx):
        """Display the list of confirmed squads for a mogi; sends 15 at a time to avoid
           reaching 2000 character limit"""
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if not await self.is_started(ctx, mogi):
            return
        mogi_list = mogi.confirmed_list()
        if len(mogi_list) == 0:
            await ctx.send(f"There are no squads in the mogi - confirm {mogi.size} players to join")
            return
        sorted_mogi_list = sorted(mogi_list, reverse=True)
        msg = f"`SQ #{mogi.sq_id} Mogi List`\n"
        for i in range(len(sorted_mogi_list)):
            if len(msg) > 1500:
                await ctx.send(msg)
                msg = ""
            msg += f"`{i+1}.` "
            msg += ", ".join([p.lounge_name for p in sorted_mogi_list[i].players])
            msg += f" ({sorted_mogi_list[i].avg_mmr:.1f} MMR)\n"
        if(len(sorted_mogi_list) % (12/mogi.size) != 0):
            num_next = int(len(sorted_mogi_list) % (12/mogi.size))
            teams_per_room = int(12/mogi.size)
            num_rooms = int(len(sorted_mogi_list) / (12/mogi.size))+1
            msg += f"`[{num_next}/{teams_per_room}] teams for {num_rooms} rooms`"
        await ctx.send(msg)
        

    async def start_input_validation(self, ctx, size:int, sq_id:int):
        valid_sizes = [1, 2, 3, 4, 6]
        if size not in valid_sizes:
            await(await ctx.send(f"The size you entered is invalid; proper values are: {', '.join(valid_sizes)}")).delete(delay=5)
            return False
        return True
        
    #check if user has roles defined in config.json
    async def has_roles(self, member:discord.Member, guild_id:int, config):
        if str(guild_id) not in config["admin_roles"].keys():
            return True
        for role in member.roles:
            if role.name in config["admin_roles"][str(guild_id)]:
                return True
        return False

    @commands.command()
    @commands.guild_only()
    async def start(self, ctx, size:int, sq_id:int):
        """Start a mogi in the current channel"""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        if not await self.start_input_validation(ctx, size, sq_id):
            return
        if ctx.channel in self.ongoing_events.keys():
            await ctx.send("There is already a mogi happening in this channel, so you can't use this command")
            return
        m = Mogi(sq_id, size, ctx.channel)
        m.started = True
        m.gathering = True
        self.ongoing_events[ctx.channel] = m
        await ctx.send(f"A {size}v{size} mogi has been started - @here Type `!c`, `!d`, or `!list`")
        
    @commands.command()
    @commands.guild_only()
    async def close(self, ctx):
        """Close the mogi so players can't join or drop"""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        mogi.gathering = False
        mogi.is_automated = False
        await self.lockdown(ctx.channel)
        await ctx.send("Mogi is now closed; players can no longer join or drop from the event")

    async def endMogi(self, mogi_channel):
        mogi = self.ongoing_events[mogi_channel]
        for room in mogi.rooms:
            if room.thread is None:
                return
            if not room.thread.archived:
                try:
                    await room.thread.edit(archived=True, locked=True)
                except Exception as e:
                   pass
            elif not room.thread.locked:
                try:
                    await room.thread.edit(locked=True)
                except Exception as e:
                    pass
        del self.ongoing_events[mogi_channel]

    @commands.command()
    @commands.guild_only()
    async def end(self, ctx):
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        #for room in mogi.rooms:
        #    if room.thread is not None:
        #        await room.thread.edit(archived=True, locked=True)
        #del self.ongoing_events[ctx.channel]
        await self.endMogi(mogi.mogi_channel)
        await ctx.send(f"{ctx.author.display_name} has ended the mogi")
        

    @commands.command()
    @commands.guild_only()
    async def open(self, ctx):
        """Close the mogi so players can't join or drop"""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
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
        await self.unlockdown(ctx.channel)
        await ctx.send("Mogi is now open; players can join and drop from the event")

    #command to add staff to room thread channels; users can't add new users to private threads,
    #so the bot has to with this command
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def staff(self, ctx):
        """Calls staff to the current channel. Only works in thread channels for SQ rooms."""
        is_room_thread = False
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                is_room_thread = True
                break
        if not is_room_thread:
            return
        if str(ctx.guild.id) not in ctx.bot.config["staff_roles"].keys():
            await ctx.send("There is no Lounge Staff role configured for this server")
            return
        lounge_staff_roles = ctx.bot.config["staff_roles"][str(ctx.guild.id)]
        mentions = " ".join([ctx.guild.get_role(role).mention for role in lounge_staff_roles])
        await ctx.send(mentions)

    #@commands.command()
    async def fc(self, ctx, *, name=None):
        """Displays the FC for the given player. Only works in thread channels for SQ rooms."""
        is_room_thread = False
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                is_room_thread = True
                break
        if not is_room_thread:
            return
        if name is None:
            name = ctx.author.display_name
        player_fc = await mk8dx_150cc_fc(self.bot.config, name)
        if player_fc is not None:
            await ctx.send(player_fc)
        else:
            await ctx.send("Player not found!")

    @commands.command()
    async def scoreboard(self, ctx):
        """Displays the scoreboard of the room. Only works in thread channels for SQ rooms."""
        is_room_thread = False
        room = None
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                room = mogi.get_room_from_thread(ctx.channel.id)
                is_room_thread = True
                break
        if not is_room_thread:
            return
        msg = "`#RESULTS\n"
        for i, team in enumerate(room.teams):
            msg += f"Team {i+1} - {chr(ord('A')+i)}\n"
            for player in team.players:
                msg += f"{player.lounge_name} [] 0\n"
            msg += "\n"
        msg += f"`Fill out the scores for each player and then use the `!submit` command to submit the table."
        await ctx.send(msg)

    @commands.command()
    async def lt(self, ctx):
        is_room_thread = False
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                is_room_thread = True
                break
        if not is_room_thread:
            return
        await ctx.send("Stats bot cannot read messages in threads, so this command will not work. Please use `!scoreboard` to make the table.")
        
    async def makeRoomsLogic(self, mogi, open_time:int, started_automatically=False):
        if open_time >= 60 or open_time < 0:
            await mogi.mogi_channel.send("Please specify a valid time (in minutes) for rooms to open (00-59)")
            return
        if mogi.making_rooms_run and started_automatically:
            return
        num_rooms = int(mogi.count_registered() / (12/mogi.size))
        if num_rooms == 0:
            await mogi.mogi_channel.send(f"Not enough players to fill a room! Try this command with at least {int(12/mogi.size)} teams")
            return
        await self.lockdown(mogi.mogi_channel)
        mogi.making_rooms_run = True
        if mogi.gathering:
            mogi.gathering = False
            await mogi.mogi_channel.send("Mogi is now closed; players can no longer join or drop from the event")
        
        pen_time = open_time + 5
        start_time = open_time + 10
        while pen_time >= 60:
            pen_time -= 60
        while start_time >= 60:
            start_time -= 60
        teams_per_room = int(12/mogi.size)
        num_teams = int(num_rooms * teams_per_room)
        final_list = mogi.confirmed_list()[0:num_teams]
        sorted_list = sorted(final_list, reverse=True)

        extra_members = []
        if str(mogi.mogi_channel.guild.id) in self.bot.config["members_for_channels"].keys():
            extra_members_ids = self.bot.config["members_for_channels"][str(mogi.mogi_channel.guild.id)]
            for m in extra_members_ids:
                extra_members.append(mogi.mogi_channel.guild.get_member(m))
    
        rooms = []
        mogi.rooms = rooms
        for i in range(num_rooms):
            if i > 0 and i % 50 == 0:
                await mogi.mogi_channel.send("Additional rooms will be created in 3-5 minutes.")
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
                #scoreboard += ",".join([p.lounge_name for p in team.players])
                #if j+1 < teams_per_room:
                #    scoreboard += ","
            room_msg = msg
            mentions += " ".join([m.mention for m in extra_members if m is not None])
            room_msg += f"{scoreboard}\n"
            room_msg += ("\nDecide a host amongst yourselves; room open at :%02d, penalty at :%02d, start by :%02d. Good luck!\n\n"
                        % (open_time, pen_time, start_time))
            room_msg += "\nIf you need staff's assistance, use the `!staff` command in this channel.\n"
            room_msg += mentions
            thread_type = 1
            try: 
                #can only make private threads in servers with boost level 2 LOL!
                if mogi.mogi_channel.guild.premium_tier >= 2:
                    room_channel = await mogi.mogi_channel.create_thread(name=room_name,
                                                                         auto_archive_duration=60,
                                                                         invitable=False)
                else:
                    thread_msg = await mogi.mogi_channel.send(msg)
                    room_channel = await mogi.mogi_channel.create_thread(name=room_name,
                                                                         message=thread_msg,
                                                                         auto_archive_duration=60)
                    thread_type = 0
                await room_channel.send(room_msg)
            except Exception as e:
                print(e)
                err_msg = f"\nAn error has occurred while creating the room channel; please contact your opponents in DM or another channel\n"
                err_msg += mentions
                msg += err_msg
                room_channel = None
            rooms.append(Room(sorted_list[start_index:start_index+teams_per_room],
                              i+1, room_channel))
            if thread_type == 1:
                await mogi.mogi_channel.send(msg)
        mogi.rooms = rooms
        if num_teams < mogi.count_registered():
            missed_teams = mogi.confirmed_list()[num_teams:mogi.count_registered()]
            msg = "`Late teams:`\n"
            for i in range(len(missed_teams)):
                msg += f"`{i+1}.` "
                msg += ", ".join([p.lounge_name for p in missed_teams[i].players])
                msg += f" ({int(missed_teams[i].avg_mmr)} MMR)\n"
            await mogi.mogi_channel.send(msg)

    @commands.command()
    @commands.guild_only()
    @commands.max_concurrency(number=1, wait=True)
    async def makeRooms(self, ctx, openTime:int):
        """Makes thread channels for SQ rooms."""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)):
            return
        await self.makeRoomsLogic(mogi, openTime)
        
    async def scheduler_mogi_start(self):
        cur_time = datetime.now()
        for guild in self.scheduled_events.values():
            to_remove = [] #Keep a list of indexes to remove - can't remove while iterating
            for i, mogi in enumerate(guild):
                if(mogi.start_time - self.QUEUE_OPEN_TIME) < cur_time:
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

    async def check_num_teams(self, mogi):
        if not mogi.gathering or not mogi.is_automated:
            return
        cur_time = datetime.now()
        if mogi.start_time - self.QUEUE_OPEN_TIME + self.JOINING_TIME <= cur_time:
            numLeftoverTeams = mogi.count_registered() % int((12/mogi.size))
            if numLeftoverTeams == 0:
                mogi.gathering = False
                await self.lockdown(mogi.mogi_channel)
                await mogi.mogi_channel.send("A sufficient amount of teams has been reached, so the mogi has been closed to extra teams. Rooms will be made within the next minute.")


    async def ongoing_mogi_checks(self):
        for mogi in self.ongoing_events.values():
            #If it's not automated, not started, we've already started making the rooms, don't run this
            if not mogi.is_automated or not mogi.started or mogi.making_rooms_run:
                return
            cur_time = datetime.now()
            if (mogi.start_time - self.QUEUE_OPEN_TIME + self.JOINING_TIME + self.EXTENSION_TIME) <= cur_time:
                await self.makeRoomsLogic(mogi, (mogi.start_time.minute)%60, True)
                return
            if mogi.start_time - self.QUEUE_OPEN_TIME + self.JOINING_TIME <= cur_time:
                #check if there are an even amount of teams since we are past the queue time
                numLeftoverTeams = mogi.count_registered() % int((12/mogi.size))
                if numLeftoverTeams == 0:
                    await self.makeRoomsLogic(mogi, (mogi.start_time.minute)%60, True)
                    return
                else:
                    if int(cur_time.second / 20) == 0:
                        force_time = mogi.start_time - self.QUEUE_OPEN_TIME + self.JOINING_TIME + self.EXTENSION_TIME
                        minutes_left = int((force_time - cur_time).seconds/60)
                        x_teams = int(int(12/mogi.size) - numLeftoverTeams)
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
    #@app_commands.guilds(445404006177570829)
    async def get_time_command(self, interaction:discord.Interaction,
                    schedule_time:str, timezone:str):
        """Get the Discord timestamp string for a time"""
        actual_time = self.getTime(schedule_time, timezone)
        event_str = discord.utils.format_dt(actual_time, style="F")
        await interaction.response.send_message(f"`{event_str}`", ephemeral=True)

    @app_commands.command(name="schedule_event")
    @app_commands.choices(
        size=[
            Choice(name="FFA", value=1),
            Choice(name="2v2", value=2),
            Choice(name="3v3", value=3),
            Choice(name="4v4", value=4),
            Choice(name="6v6", value=6)
            ])
    #@app_commands.guilds(445404006177570829)
    async def schedule_event(self, interaction:discord.Interaction,
                       size:Choice[int], sq_id: int,
                             channel:discord.TextChannel,
                       schedule_time:str, timezone:str):
        """Schedules an SQ event in the given channel at the given time."""
        if not await self.has_roles(interaction.user, interaction.guild_id, self.bot.config):
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
        
        event_start_time = actual_time.astimezone() - self.QUEUE_OPEN_TIME
        event_end_time = event_start_time + self.JOINING_TIME
        if event_end_time < discord.utils.utcnow():
            bad_time = discord.utils.format_dt(event_end_time, style="F")
            await interaction.response.send_message("The queue for this event would end in the past! "
            f"({bad_time}) "
            "Make sure your timezone is correct (with daylight savings taken into account, "
            "ex. EDT instead of EST if it's summer), and that you've entered the date if it's not today")
            return
        if event_start_time < discord.utils.utcnow():
            #have to add 1 minute here, because utcnow() will technically be the past when the API request is sent
            event_start_time = discord.utils.utcnow() + timedelta(minutes=1)
        discord_event = await interaction.guild.create_scheduled_event(name=f"SQ #{sq_id}: {size.name} gathering players",
                                                       start_time = event_start_time,
                                                       end_time = event_end_time,
                                                       entity_type = discord.EntityType.external,
                                                       location=channel.mention)
        mogi = Mogi(sq_id, size.value, channel, is_automated=True, start_time=actual_time, discord_event=discord_event)
        if interaction.guild not in self.scheduled_events.keys():
            self.scheduled_events[interaction.guild] = []
        self.scheduled_events[interaction.guild].append(mogi)
        event_str = self.get_event_str(mogi)
        await interaction.response.send_message(f"Scheduled the following event:\n{event_str}")

    def get_event_str(self, mogi):
        mogi_time = discord.utils.format_dt(mogi.start_time, style="F")
        return(f"`#{mogi.sq_id}` **{mogi.size}v{mogi.size}:** {mogi_time}")

    @app_commands.command(name="remove_event")
    #@app_commands.guilds(445404006177570829)
    async def remove_event(self, interaction:discord.Interaction, event_id:int, channel:discord.TextChannel):
        """Removes an event from the schedule"""
        if not await self.has_roles(interaction.user, interaction.guild_id, self.bot.config):
            await interaction.response.send_message("You do not have permissions to use this command",ephemeral=True)
            return
        if interaction.guild not in self.scheduled_events.keys():
            await interaction.response.send_message("This event number isn't in the schedule. Do `!view_schedule` to see the scheduled events.",
                                                    ephemeral=True)
            return
        for event in self.scheduled_events[interaction.guild]:
            if event.sq_id == event_id and event.mogi_channel == channel:
                self.scheduled_events[interaction.guild].remove(event)
                #await event.discord_event.cancel()
                await event.discord_event.edit(status=discord.EventStatus.cancelled, end_time=event.discord_event.end_time, location=event.discord_event.location)
                await interaction.response.send_message(f"Removed the following event:\n{self.get_event_str(event)}")
                return
        await interaction.response.send_message("This event number isn't in the schedule. Do `!view_schedule` to see the scheduled events.")

    @commands.command()
    @commands.guild_only()
    async def view_schedule(self, ctx, copy_paste=""):
        """View the SQ schedule. Use !view_schedule cp to get a copy/pastable version"""
        if ctx.guild not in self.scheduled_events.keys():
            await ctx.send("There are no SQ events scheduled in this server yet. Use /schedule_event to schedule one.")
        server_schedule = self.scheduled_events[ctx.guild]
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

    @commands.command(aliases=['pt'])
    async def parsetime(self, ctx, *, schedule_time:str):
        try:
            actual_time = parse(schedule_time)
            await ctx.send("```<t:" + str(int(time.mktime(actual_time.timetuple()))) + ":F>```")
        except (ValueError, OverflowError):
            await ctx.send("I couldn't figure out the date and time for your event. Try making it a bit more clear for me.")

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync(self, ctx):
        await self.bot.tree.sync()
        await ctx.send("sync'd")

    @commands.command(name="sync_server")
    @commands.is_owner()
    async def sync_server(self, ctx):
        await self.bot.tree.sync(guild=discord.Object(id=445404006177570829))
        await ctx.send("sync'd")

    #@commands.command()
    async def get_bots(self, ctx):
        extra_members = []
        if str(ctx.guild.id) in self.bot.config["members_for_channels"].keys():
            extra_members_ids = self.bot.config["members_for_channels"][str(ctx.guild.id)]
            for m in extra_members_ids:
                extra_members.append(ctx.guild.get_member(m))
            for m in extra_members:
                print(m)

    #@commands.command()
    async def thread_test(self, ctx):
        for i in range(100):
            thread_msg = await ctx.send(f"{i+1}")
            room_channel = await ctx.channel.create_thread(name=f"Room {i+1}",
                                                                    message=thread_msg,
                                                                    auto_archive_duration=60)
            await asyncio.sleep(2)


async def setup(bot):
    await bot.add_cog(SquadQueue(bot))
