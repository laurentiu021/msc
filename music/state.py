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

        # Token de generatie pentru callback-ul after_play. VoiceClient.stop()
        # declanseaza ALWAYS callback-ul, deci un stop deliberat (seek, nplay,
        # inlocuirea piesei) facea coada sa avanseze si stergea fisierul care
        # tocmai pornea. Cine opreste intentionat incrementeaza generatia;
        # callback-ul vechi vede alt numar si iese fara sa faca nimic.
        self.play_generation = 0

        # Pauza dupa 5 erori consecutive. Fara ea, timer-ul de 24/7 reactiva
        # autoplay la fiecare 60s si intrerupatorul nu putea tine niciodata.
        self.breaker_until = 0.0

        # Pauza dupa un prefill de autoplay care n-a intors nimic, ca sa nu
        # batem YouTube-ul din minut in minut cand ne blocheaza.
        self.idle_quiet_until = 0.0

        # Ultimul mesaj de eroare BRUT de la yt-dlp. Mesajele noastre in romana
        # il inlocuiau inainte sa ajunga la diagnoza, deci fiecare eroare ieseau
        # ca "Eroare necunoscuta" si sfatul despre cookies nu putea fi afisat.
        self.last_raw_error: str | None = None

        # View-ul curent, ca sa poata fi oprit inainte de a fi inlocuit.
        # Message.delete() nu il scoate din ViewStore-ul lui discord.py.
        self.current_view = None


guild_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]
