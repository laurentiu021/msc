"""GuildState si state management."""
import asyncio


class GuildState:
    """Starea per-guild: coada, history, flags."""
    def __init__(self):
        self.queue: list[dict] = []
        self.history: list[dict] = []
        self.autoplay = False
        self.loop_mode = 0  # 0=off, 1=piesa, 2=coada
        self.show_queue = False

        self.last_title = ""
        self.last_url = None
        self.last_duration = 0
        self.last_thumbnail = None
        self.last_channel = ""
        self.last_views = 0
        self.last_likes = 0
        self.is_radio_now = False

        self.current_msg = None
        self.is_loading = False
        self.last_start_time = 0
        self.timeout_task = None
        self.skip_request = False
        self._lock = asyncio.Lock()
        self.current_file = None
        self.always_on = False
        self.preloaded: dict | None = None
        self._consecutive_errors = 0
        self._last_notified_error: str | None = None


guild_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]
