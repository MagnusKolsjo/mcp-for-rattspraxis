#!/usr/bin/env python3
"""
01_synka_publiceringar.py — fyller avgorande_cache med Domstolsverkets
publiceringar, så att sok_i_domtext söker i hela korpusen.

Hämtar GET /publiceringar sorterat på publiceringstid, äldst först, 100 per
sida, och skriver varje sida till databasen i en transaktion. Läget sparas i
tabellen synk_status efter varje sida:

  - Första körningen (inget sparat läge) är en fullsynk av hela korpusen.
  - Därefter hämtas bara publiceringar från och med dagen för den senast
    hämtade publiceringen. Den dagen hämtas om, eftersom API:et bara tar
    datum och inte klockslag; skrivningen är en upsert, så dubbletter uppstår
    inte.
  - En avbruten körning fortsätter vid nästa körning där den slutade.

Med --med-pdf (eller --bara-pdf) följer ett andra steg: PDF-bilagorna till
publiceringar som saknar HTML-fulltext (domar och beslut från bland andra HD
och MÖD) hämtas, texten extraheras och lagras i pdf_cache, där sok_i_domtext
söker. Varje PDF lagras för sig, så steget kan avbrytas och fortsätter med
de PDF:er som återstår.

Publiceringar som ändras hos källan utan att få ny publiceringstid fångas
inte av den inkrementella synken. De uppdateras när hamta_avgorande hämtar
dem på nytt efter cachens TTL, eller vid en ny fullsynk (--alla).

Användning:
    python3 01_synka_publiceringar.py                    # inkrementellt
    python3 01_synka_publiceringar.py --sedan 2026-09-01 # från ett datum
    python3 01_synka_publiceringar.py --alla             # fullsynk
    python3 01_synka_publiceringar.py --med-pdf          # även PDF-texterna
    python3 01_synka_publiceringar.py --bara-pdf         # bara PDF-steget
    python3 01_synka_publiceringar.py --installera-schema

Konfiguration via .env — se config.example.env.
"""

import argparse
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

import db  # noqa: E402
import klient  # noqa: E402
import pdftext  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("synk")

JOBB = "publiceringar"
JOBB_PDF = "pdf"

# Paus mellan sidanropen. Varje sida är 1–2 MB; två sekunder håller
# belastningen på källan låg även under en fullsynk.
PAUS_SEKUNDER = float(os.getenv("RP_SYNK_PAUS_SEKUNDER", "2"))

# Paus mellan PDF-hämtningarna. En PDF är typiskt några hundra kB.
PDF_PAUS_SEKUNDER = float(os.getenv("RP_SYNK_PDF_PAUS_SEKUNDER", "2"))

# Väntetider före nya försök när källan svarar med fel eller inte alls.
_OMFORSOK_SEKUNDER = (10, 30, 90)


def _nu() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hamta_sida(fran: str | None, sida: int) -> list[dict]:
    """Hämtar en sida, med nya försök vid tillfälliga fel hos källan."""
    for forsok, vanta in enumerate((*_OMFORSOK_SEKUNDER, None), start=1):
        try:
            return klient.lista_publiceringar(fran, sida)
        except klient.KallaFel as e:
            if vanta is None:
                raise
            log.warning("Sida %d, försök %d misslyckades: %s Väntar %d s.",
                        sida, forsok, e, vanta)
            time.sleep(vanta)
    return []


def _kontrollera_tackning() -> None:
    """Jämför antalet lokala avgöranden med källans totalsiffra och loggar."""
    try:
        total = klient.sok({
            "sokfras": {"andLista": []}, "filter": {},
            "sidIndex": 0, "antalPerSida": 1,
        }).get("total")
    except klient.KallaFel as e:
        log.warning("Källans totalsiffra kunde inte hämtas: %s", e)
        return
    lokalt = db.rakna_lokalt()["avgoranden"]
    log.info("Täckning: %d avgöranden lokalt, %s hos källan", lokalt, total)
    if total and lokalt < total:
        log.warning(
            "%d publiceringar saknas lokalt. Kör om med --alla för att komplettera.",
            total - lokalt,
        )


def synka(sedan: str | None = None, alla: bool = False, max_sidor: int | None = None) -> int:
    """
    Synkar publiceringar från källan. Returnerar antal skrivna publiceringar.

    `sedan` anger publicerad_fran_och_med (ÅÅÅÅ-MM-DD). Utan `sedan` och
    `alla` fortsätter synken från sparat läge, eller gör en fullsynk om
    inget läge finns.
    """
    db._sakerstall_schema()
    lage = db.las_synk_status(JOBB)

    # senaste följer hur långt den här körningen kommit. Bara när synken
    # fortsätter från sparat läge utgår den från det sparade värdet; efter
    # --sedan eller --alla skulle ett äldre sparat värde annars dölja en lucka
    # om körningen avbryts.
    senaste = None
    if alla:
        fran = None
    elif sedan:
        fran = sedan
    elif lage and lage.get("senaste_publiceringstid"):
        senaste = lage["senaste_publiceringstid"]
        fran = senaste[:10]
    else:
        fran = None
    fullsynk = fran is None

    log.info("Synkar publiceringar %s",
             "från början (fullsynk)" if fullsynk else f"publicerade från och med {fran}")
    db.spara_synk_status(
        JOBB, status="pagar", startad=_nu(), fran_datum=fran,
        antal_sidor=0, antal_publiceringar=0, meddelande=None,
    )

    sida = 0
    antal = 0
    try:
        while max_sidor is None or sida < max_sidor:
            publiceringar = _hamta_sida(fran, sida)
            if not publiceringar:
                break
            antal += db.skriv_avgoranden(publiceringar)
            tider = [p.get("publiceringstid") for p in publiceringar if p.get("publiceringstid")]
            if tider:
                senaste = max([senaste or ""] + tider)
            sida += 1
            db.spara_synk_status(
                JOBB, antal_sidor=sida, antal_publiceringar=antal,
                senaste_publiceringstid=senaste,
            )
            if sida % 10 == 0:
                log.info("%d sidor, %d publiceringar, senast publicerad %s",
                         sida, antal, senaste)
            if len(publiceringar) < klient.MAX_SIDSTORLEK_PUBLICERINGAR:
                break
            time.sleep(PAUS_SEKUNDER)
    except Exception as e:
        # avslutad lämnas orörd: den anger när den senaste lyckade körningen
        # blev klar, och sok_i_domtext bedömer täckningen utifrån den.
        db.spara_synk_status(
            JOBB, status="fel", meddelande=f"{_nu()}: {e}"[:500],
        )
        log.error("Synken avbröts efter %d sidor: %s", sida, e)
        raise

    avslutad_i_fortid = max_sidor is not None and sida >= max_sidor
    if avslutad_i_fortid:
        klar = {"status": "avbruten"}
    else:
        klar = {"status": "klar", "avslutad": _nu()}
        if fullsynk:
            klar["fullsynk_klar"] = klar["avslutad"]
    db.spara_synk_status(JOBB, **klar)
    log.info("Klart — %d publiceringar på %d sidor, senast publicerad %s",
             antal, sida, senaste)
    if fullsynk and not avslutad_i_fortid:
        _kontrollera_tackning()
    return antal


def _hamta_bilaga(fillagring_id: str) -> bytes:
    """Hämtar en PDF, med nya försök vid tillfälliga fel hos källan."""
    for forsok, vanta in enumerate((*_OMFORSOK_SEKUNDER, None), start=1):
        try:
            return klient.hamta_bilaga(fillagring_id)
        except klient.AvgorandeSaknas:
            raise
        except klient.KallaFel as e:
            if vanta is None:
                raise
            log.warning("PDF %s, försök %d misslyckades: %s Väntar %d s.",
                        fillagring_id, forsok, e, vanta)
            time.sleep(vanta)
    return b""


def synka_pdf(max_antal: int | None = None) -> int:
    """
    Hämtar och lagrar texten i PDF-bilagor som ännu saknar text.

    En PDF som saknas hos källan eller inte går att läsa hoppas över och
    loggas; den försöks igen vid nästa körning. Svarar källan inte alls
    avbryts steget, så att nästa körning tar vid. Datumet för senaste
    lyckade körning sparas bara när steget gått igenom hela listan.
    Returnerar antal lagrade texter.
    """
    db._sakerstall_schema()
    att_hamta = db.pdf_att_hamta(max_antal)
    log.info("PDF-steget: %d bilagor saknar text", len(att_hamta))
    # I synk_status för PDF-steget anger antal_publiceringar antal lagrade
    # PDF-texter i den senaste körningen.
    db.spara_synk_status(
        JOBB_PDF, status="pagar", startad=_nu(),
        antal_sidor=0, antal_publiceringar=0, meddelande=None,
    )

    lagrade = 0
    hoppade: list[str] = []
    byte_totalt = 0
    try:
        for nr, bilaga in enumerate(att_hamta, start=1):
            fid = bilaga["fillagring_id"]
            try:
                pdf_bytes = _hamta_bilaga(fid)
                text = pdftext.extrahera_text(pdf_bytes)
            except (klient.AvgorandeSaknas, pdftext.PdfFel) as e:
                log.warning("Hoppar över %s: %s", fid, e)
                hoppade.append(fid)
                continue
            db._skriv_pdf_cache(
                fillagring_id=fid, text_md=text,
                avgorande_id=bilaga["avgorande_id"],
                filstorlek=len(pdf_bytes), filnamn=bilaga["filnamn"], kasta=True,
            )
            lagrade += 1
            byte_totalt += len(pdf_bytes)
            if nr % 25 == 0:
                log.info("%d av %d PDF:er, %d lagrade, %.1f MB",
                         nr, len(att_hamta), lagrade, byte_totalt / 1e6)
                db.spara_synk_status(JOBB_PDF, antal_publiceringar=lagrade)
            if nr < len(att_hamta):
                time.sleep(PDF_PAUS_SEKUNDER)
    except Exception as e:
        db.spara_synk_status(
            JOBB_PDF, status="fel", antal_publiceringar=lagrade,
            meddelande=f"{_nu()}: {e}"[:500],
        )
        log.error("PDF-steget avbröts efter %d lagrade texter: %s", lagrade, e)
        raise

    klar: dict = {"antal_publiceringar": lagrade}
    if max_antal is not None and len(att_hamta) >= max_antal:
        klar["status"] = "avbruten"
    else:
        klar.update(status="klar", avslutad=_nu())
    if hoppade:
        klar["meddelande"] = f"{len(hoppade)} PDF:er hoppades över, t.ex. {hoppade[0]}"
    db.spara_synk_status(JOBB_PDF, **klar)
    log.info("PDF-steget klart — %d texter lagrade (%.1f MB PDF), %d överhoppade",
             lagrade, byte_totalt / 1e6, len(hoppade))
    return lagrade


# ---------------------------------------------------------------------------
# Schemaläggning
# ---------------------------------------------------------------------------

def installera_schema() -> None:
    """Installerar daglig schemaläggning via launchd (macOS) eller cron (Linux)."""
    schemalaggare = os.environ.get("SCHEMALAGGARE", "launchd").lower()
    cron_schema = os.environ.get("CRON_SCHEMA", "15 4 * * *")
    wrapper = _SCRIPT_DIR / "synk_daglig.sh"
    if not wrapper.exists():
        log.error("synk_daglig.sh saknas i %s", _SCRIPT_DIR)
        sys.exit(1)
    if schemalaggare == "launchd":
        _installera_launchd(wrapper, cron_schema)
    elif schemalaggare == "cron":
        _installera_cron(wrapper, cron_schema)
    else:
        log.error("Okänd schemaläggare: %s (giltiga: launchd, cron)", schemalaggare)
        sys.exit(1)


def _installera_launchd(wrapper: Path, cron_schema: str) -> None:
    """Skapar en plist i ~/Library/LaunchAgents och laddar in den."""
    minut, timme, *_ = cron_schema.split()
    label = f"se.magnuskolsjo.{_SCRIPT_DIR.name}.synk"
    plist_sokvag = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    (_SCRIPT_DIR / "logs").mkdir(exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{escape(label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{escape(str(wrapper))}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{int(timme)}</integer>
        <key>Minute</key><integer>{int(minut)}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>{escape(str(_SCRIPT_DIR))}/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key><string>{escape(str(_SCRIPT_DIR))}/logs/launchd-stderr.log</string>
</dict>
</plist>
"""
    plist_sokvag.parent.mkdir(parents=True, exist_ok=True)
    plist_sokvag.write_text(plist)
    subprocess.run(["launchctl", "unload", str(plist_sokvag)], check=False)
    subprocess.run(["launchctl", "load", str(plist_sokvag)], check=True)
    log.info("launchd-jobb installerat: %s", plist_sokvag)
    log.info("Körs varje dag kl %s:%02d", timme, int(minut))


def _installera_cron(wrapper: Path, cron_schema: str) -> None:
    """Lägger till en rad i användarens crontab, om den inte redan finns."""
    # Sökvägen citeras: servermappen kan innehålla blanksteg.
    rad = f"{cron_schema} /bin/bash {shlex.quote(str(wrapper))}\n"
    befintlig = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, check=False
    ).stdout
    if str(wrapper) in befintlig:
        log.info("cron-rad finns redan — uppdaterar inte")
        return
    subprocess.run(["crontab", "-"], input=befintlig + rad, text=True, check=True)
    log.info("cron-rad tillagd: %s", rad.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synkar Domstolsverkets publiceringar till avgorande_cache.",
    )
    parser.add_argument("--sedan", help="Hämta publiceringar publicerade från och med ÅÅÅÅ-MM-DD.")
    parser.add_argument("--alla", action="store_true",
                        help="Fullsynk: hämta hela korpusen oavsett sparat läge.")
    parser.add_argument("--max-sidor", type=int,
                        help="Avbryt efter så många sidor (för test).")
    parser.add_argument("--med-pdf", action="store_true",
                        help="Hämta även PDF-texterna för publiceringar utan HTML-fulltext.")
    parser.add_argument("--bara-pdf", action="store_true",
                        help="Kör bara PDF-steget, inte synken av publiceringar.")
    parser.add_argument("--max-pdf", type=int,
                        help="Hämta högst så många PDF:er i PDF-steget (för test).")
    parser.add_argument("--installera-schema", action="store_true",
                        help="Installera daglig körning via launchd eller cron.")
    args = parser.parse_args()

    if args.installera_schema:
        installera_schema()
        return

    if args.sedan:
        try:
            datetime.strptime(args.sedan, "%Y-%m-%d")
        except ValueError:
            parser.error("--sedan ska vara ett datum på formen ÅÅÅÅ-MM-DD")
    if not db.DATABASE_URL:
        log.error("DATABASE_URL saknas i .env — synken behöver en databas.")
        sys.exit(1)

    if not args.bara_pdf:
        synka(sedan=args.sedan, alla=args.alla, max_sidor=args.max_sidor)
    if args.med_pdf or args.bara_pdf:
        synka_pdf(max_antal=args.max_pdf)


if __name__ == "__main__":
    main()
