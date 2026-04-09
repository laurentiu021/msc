"""Diagnoza erorilor yt-dlp — mesaje user-friendly pentru Discord."""


def diagnose_error(error_str: str) -> tuple[str, str]:
    """Analizeaza eroarea si returneaza (tip_eroare, mesaj_discord).

    tip_eroare e un key scurt folosit pt de-duplicare (sa nu spammeze).
    mesaj_discord e textul trimis pe Discord.
    """
    e = str(error_str).lower()

    if "sign in to confirm" in e or "cookies" in e:
        return ("cookies", (
            "🍪 **Cookies YouTube expirate!**\n"
            "YouTube crede ca sunt bot si cere autentificare.\n"
            "➡️ Trebuie sa reinnoiesti cookies-urile:\n"
            "1. Exporta cookies din browser (extensia *Get cookies.txt LOCALLY*)\n"
            "2. Intra pe Railway → Variables → `YT_COOKIES_CONTENT`\n"
            "3. Lipeste continutul nou si da Redeploy"
        ))

    if "http error 429" in e or "too many requests" in e or "rate limit" in e:
        return ("ratelimit", (
            "⏳ **YouTube m-a limitat temporar (rate limit).**\n"
            "Prea multe cereri intr-un timp scurt.\n"
            "➡️ Asteapta cateva minute si incearca din nou."
        ))

    if "video unavailable" in e or "is not available" in e:
        return ("unavailable", (
            "🚫 **Video-ul nu e disponibil.**\n"
            "Poate fi sters, privat, sau blocat in regiunea serverului."
        ))

    if "age" in e and ("confirm" in e or "verify" in e or "gate" in e):
        return ("age_gate", (
            "🔞 **Video-ul cere verificare de varsta.**\n"
            "YouTube nu il lasa fara cookies de cont.\n"
            "➡️ Reinnoieste cookies-urile pe Railway (la fel ca la eroarea de cookies)."
        ))

    if "no video formats" in e or "requested format" in e:
        return ("format", (
            "📦 **Nu am gasit un format audio compatibil.**\n"
            "Video-ul poate fi live, premium, sau intr-un format ciudat.\n"
            "➡️ Daca se repeta la toate melodiile, PO Token server-ul poate fi cazut.\n"
            "Verifica logurile pe Railway."
        ))

    if "po token" in e or "403" in e or "forbidden" in e:
        return ("po_token", (
            "🔑 **Eroare PO Token / Acces interzis (403).**\n"
            "YouTube a blocat cererea. PO Token server-ul poate fi cazut.\n"
            "➡️ Verifica pe Railway ca containerul ruleaza corect.\n"
            "In loguri cauta `[STARTUP] PO Token server running`."
        ))

    if "urlopen error" in e or "timed out" in e or "connection" in e:
        return ("network", (
            "🌐 **Eroare de retea.**\n"
            "Serverul nu poate ajunge la YouTube momentan.\n"
            "➡️ De obicei se rezolva singur. Daca persista, verifica statusul Railway."
        ))

    if "ffmpeg" in e or "opus" in e:
        return ("ffmpeg", (
            "🔧 **Eroare la procesarea audio (FFmpeg).**\n"
            "Fisierul descarcat pare corupt sau FFmpeg are o problema.\n"
            "➡️ Incearca alta melodie. Daca se repeta, spune-mi exact eroarea."
        ))

    # Eroare necunoscuta — afiseaza un fragment din eroare
    short = str(error_str)[:200]
    return ("unknown", (
        f"❓ **Eroare necunoscuta:**\n"
        f"`{short}`\n"
        f"➡️ Trimite-mi mesajul asta sa vad ce s-a intamplat."
    ))
