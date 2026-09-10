"""GuildState si state management."""
import asyncio
import contextlib


class GuildState:
    """Starea per-guild: coada, history, flags."""
    def __init__(self):
        self.queue: list[dict] = []
        self.history: list[dict] = []
        self.autoplay = False
        # True doar cand radioul a fost oprit DELIBERAT (buton, comanda). Vezi
        # set_autoplay: tick-ul de 24/7 il consulta ca sa nu-l reporneasca.
        self.autoplay_user_off = False
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
        # Ultima data cand a ieșit CHIAR audio din proces, adica `vc.play` a
        # reusit. Singurul semnal care raspunde la "mai merge?" fara sa cheltuie
        # nicio cerere: o sonda periodica ar arde cereri cu cookies pe exact IP-ul
        # care ne provoaca — motivul pentru care proba de la pornire e opt-in.
        # 0.0 = nu s-a redat nimic de la pornirea procesului.
        self.last_play_ok = 0.0
        # Momentul pauzei, 0 cand se reda. Fara el, panoul calcula finalul
        # ca last_start_time + durata, deci dupa o pauza de 10 minute anunta
        # ca piesa s-a terminat acum 6 minute.
        self.paused_at = 0.0
        self.timeout_task = None
        self.skip_request = False
        self._lock = asyncio.Lock()
        self.current_file = None
        self.always_on = False
        self._consecutive_errors = 0
        self._last_notified_error: str | None = None

        # Piese refuzate de reguli (live, durata) una dupa alta. Nu sunt erori,
        # deci nu trebuie sa intre in intrerupatorul de 5 erori, dar o coada
        # plina de live-uri trebuie totusi sa se opreasca la un moment dat, nu
        # sa consume o extractie completa pentru fiecare element pe rand.
        self._consecutive_rejects = 0

        # Token de generatie pentru callback-ul after_play. VoiceClient.stop()
        # declanseaza ALWAYS callback-ul, deci un stop deliberat (seek, nplay,
        # inlocuirea piesei) facea coada sa avanseze si stergea fisierul care
        # tocmai pornea. Cine opreste intentionat incrementeaza generatia;
        # callback-ul vechi vede alt numar si iese fara sa faca nimic.
        self.play_generation = 0

        # Cine deține dreptul de a elibera is_loading. Cand doua process_play se
        # suprapun, finally-ul primului stergea steagul pus de al doilea, si o
        # comanda noua putea porni o a treia redare in paralel.
        self.load_token = 0

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

        # Ultima decizie a tick-ului de inactivitate, ca sa existe un raspuns
        # la "de ce nu cânta 24/7?": fiecare ramura ieșea printr-un return mut.
        self.last_idle_reason = ''

        # View-ul curent, ca sa poata fi oprit inainte de a fi inlocuit.
        # Message.delete() nu il scoate din ViewStore-ul lui discord.py.
        self.current_view = None

        # Garda de re-intrare pentru trimiterea panoului: doua
        # actualizari concurente ar lasa doua panouri cu butoane vii.
        self._ui_sending = False


def mark_paused(state, now: float) -> None:
    """Retine momentul pauzei. Pauza nu consuma din piesa."""
    if not state.paused_at:
        state.paused_at = now


def mark_resumed(state, now: float) -> None:
    """Muta inceputul piesei cu exact cat a durat pauza."""
    if state.paused_at:
        state.last_start_time += now - state.paused_at
        state.paused_at = 0.0


def set_autoplay(state, value: bool, *, by_user: bool) -> None:
    """Comuta radioul si retine CINE l-a oprit.

    Timer-ul de 24/7 are voie sa reporneasca radioul dupa o defectiune (5 erori
    consecutive, un prefill fara rezultate), dar nu are voie sa treaca peste o
    alegere explicita. Fara distinctia asta, butonul Autoplay se stingea singur
    dupa 60 de secunde: tick-ul de 24/7 punea `autoplay = True` necondiționat, deci
    o coada curatata manual era inlocuita de un Mix de YouTube si butonul parea
    ca merge, apoi revenea in tacere.
    """
    state.autoplay = value
    if by_user:
        state.autoplay_user_off = not value


def begin_loading(state) -> int:
    """Marcheaza o incarcare in curs si intoarce token-ul proprietarului.

    Regula de proprietate exista o singura data, aici. Cand doua incarcari se
    suprapun, finally-ul primei stergea steagul pus de a doua, iar o comanda
    noua putea atunci porni o a treia in paralel.
    """
    state.is_loading = True
    state.load_token += 1
    return state.load_token


def end_loading(state, token: int) -> None:
    """Stinge steagul, dar numai daca incarcarea care il tine e a noastra."""
    if state.load_token == token:
        state.is_loading = False


@contextlib.contextmanager
def loading(state):
    """begin_loading/end_loading pentru blocuri scurte, cu acelasi contract."""
    token = begin_loading(state)
    try:
        yield token
    finally:
        end_loading(state, token)


guild_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]
