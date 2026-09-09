"""Ce face botul cand nu se reda nimic — ca functie pura.

Politica asta era singura din proiect fara nicio acoperire de teste, si singura
scrisa direct in punctul de intrare: sudata pe un asyncio.Task viu, pe un `ctx`
si pe `bot.loop`, citind stare pe care alte trei fisiere o scriu. Propriul ei
comentariu era post-mortem-ul unui bug de blocaj.

Aici nu exista ctx, nici bucla, nici import de discord: intra stare si timp, iese
o decizie. Apelantul face I/O-ul. Asta face tabelul de mai jos (vezi
tests/test_idle_policy.py) posibil fara niciun mock.
"""
from dataclasses import dataclass

# Nu face nimic in tick-ul asta (dar timer-ul se re-armeaza).
NOTHING = 'nothing'
# Ieși din canal: nimeni nu asculta si nu suntem in 24/7.
DISCONNECT = 'disconnect'
# 24/7 vrea muzica: apelantul umple coada daca e goala, apoi reda.
RADIO = 'radio'


@dataclass(frozen=True)
class IdleDecision:
    action: str
    resume_autoplay: bool = False
    reason: str = ''


def decide_idle_action(state, *, connected: bool, playing: bool, paused: bool,
                       now: float) -> IdleDecision:
    """Decizia pentru un tick de inactivitate. Fara efecte secundare.

    `now` se primeste, nu se citeste: altfel testul ar depinde de ceasul real si
    n-ar putea verifica intrerupatorul si pauza de liniste.
    """
    idle = connected and not playing and not paused

    if state.always_on:
        if not connected:
            return IdleDecision(NOTHING, reason='neconectat')
        if not idle:
            return IdleDecision(NOTHING, reason='se reda deja')
        if state.is_loading:
            return IdleDecision(NOTHING, reason='o incarcare e in curs')
        if now < state.breaker_until:
            return IdleDecision(
                NOTHING, reason=f'intrerupator activ inca {state.breaker_until - now:.0f}s')
        if now < state.idle_quiet_until:
            return IdleDecision(
                NOTHING, reason=f'pauza de liniste inca {state.idle_quiet_until - now:.0f}s')
        if not state.last_url:
            return IdleDecision(NOTHING, reason='nu s-a redat nimic inca')
        if not state.autoplay:
            if state.autoplay_user_off:
                # 24/7 inseamna "rămân in canal", nu "pornesc radioul la loc".
                # Inainte, tick-ul punea `autoplay = True` necondiționat, deci
                # butonul Autoplay se stingea singur dupa 60 de secunde si o
                # coada curatata manual era inlocuita de un Mix de YouTube.
                return IdleDecision(NOTHING, reason='radio oprit de utilizator')
            # Oprit de o defectiune (intrerupator, prefill gol), nu de om: aici
            # 24/7 chiar trebuie sa se ridice singur.
            return IdleDecision(RADIO, resume_autoplay=True,
                                reason='reluare radio dupa o defectiune')
        return IdleDecision(RADIO, reason='24/7 activ')

    if idle:
        return IdleDecision(DISCONNECT, reason='inactiv, fara 24/7')
    return IdleDecision(NOTHING, reason='se reda' if connected else 'neconectat')
