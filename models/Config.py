from dataclasses import dataclass

@dataclass
class WebsiteCredentials:
    url: str
    username: str
    password: str

@dataclass
class TimeSettings:
    queue_open_time: int # number of minutes before scheduled time of the queue that players can start joining
    joining_time: int # number of minutes after queue_open_time that players have to join the queue
    extension_time: int # number of minutes the queue can be extended to get a divisible # of teams

@dataclass
class LeaderboardConfig:
    website_credentials: WebsiteCredentials
    time_settings: TimeSettings
    players_per_mogi: int
    points_per_race: int
    races_per_mogi: int
    gps_per_mogi: int
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