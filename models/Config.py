from dataclasses import dataclass

@dataclass
class WebsiteCredentials:
    url: str
    username: str
    password: str
    game: str | None

@dataclass
class TimeSettings:
    queue_open_time: int # number of minutes before scheduled time of the queue that players can start joining
    joining_time: int # number of minutes after queue_open_time that players have to join the queue
    extension_time: int # number of minutes the queue can be extended to get a divisible # of teams

@dataclass
class LeaderboardConfig:
    name: str
    lb_name_in_string: bool # if this is true, it will show the lb name in the event string,
                                    # for example if lb name is 24p, it will say "24p 2v2" instead of "2v2"
    website_credentials: WebsiteCredentials
    time_settings: TimeSettings
    room_size: int
    valid_formats: list[int]
    join_channel: int
    list_channel: int
    pinged_member_ids: list[int] # discord IDs of members that get pinged into every room thread
    queue_messages: bool
    sec_between_queue_msgs: int
    
@dataclass
class ServerConfig:
    admin_roles: list[int]
    staff_roles: list[int]
    leaderboards: dict[str, LeaderboardConfig]

@dataclass
class BotConfig:
    token: str
    application_id: int
    servers: dict[int, ServerConfig]