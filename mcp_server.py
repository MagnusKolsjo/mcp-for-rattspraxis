#!/usr/bin/env python3
"""
MCP-server för Domstolsverkets rättspraxis-API.

Exponerar sex verktyg:
  sok_rattpraxis                 — fritextsökning med filter
  hamta_avgorande                — hämta fullständigt avgörande på ID
  hamta_pdf                      — hämta + extrahera PDF-bilaga (cachas lokalt)
  sok_rattpraxis_for_lagrum      — sök på specifik paragraf i en lag
  hamta_avgorande_pa_beteckning  — sök på NJA-nummer, HFD-referat, kortnamn, målnummer
  sok_i_domtext                  — fulltext-sökning i lokalt lagrade domtexter (FTS)

Konfiguration via .env-fil — se config.example.env.
"""

import contextlib
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Callable

from dotenv import load_dotenv

# .env läses innan db och klient importeras, eftersom de läser sin
# konfiguration vid import och servern inte ärver klientens shell-miljö.
_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pydantic import Field  # noqa: E402
from typing_extensions import NotRequired, TypedDict  # noqa: E402

import db  # noqa: E402
import klient  # noqa: E402
from db import (  # noqa: E402
    DATABASE_URL as _DATABASE_URL,
    _ar_postgres,
    _hamta_db,
    _sakerstall_schema,
    _las_avgorande_cache,
    _skriv_avgorande_cache,
    _las_pdf_cache,
    _skriv_pdf_cache,
)
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN  # noqa: E402
from mcp_transport import starta  # noqa: E402

# ---------------------------------------------------------------------------
# Inledande inställningar
# ---------------------------------------------------------------------------

_LOGS_DIR = _SCRIPT_DIR / "logs"
_LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(_LOGS_DIR / "mcp_server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# Standardtak för domtext i hamta_pdf. Utan ett tak som gäller by default kan ett
# långt avgörande överskrida MCP-protokollets storleksgräns och misslyckas helt.
# Anroparen kan alltid höja taket, eller sätta 0 för hela texten.
RP_MAX_TECKEN = int(os.getenv("RP_MAX_TECKEN", "60000"))

# sok_i_domtext räknas som heltäckande bara om en fullsynk gjorts och den
# senaste lyckade synken är högst så här många dagar gammal. Källan publicerar
# nya avgöranden varje vardag; en synk som slutat gå ger annars ett
# heltäckande-besked som tyst blir alltmer fel.
RP_TACKNING_MAX_DAGAR = int(os.getenv("RP_TACKNING_MAX_DAGAR", "3"))

# Maximalt antal träffar per API-sida — API:et tillåter upp till 50.
# Värdet är ett designval: 50 balanserar svarstid mot täckning vid paginering.
_MAX_PER_SIDA = 50

# ---------------------------------------------------------------------------
# Domstolsalias — hanterar historiska namnbyten
# ---------------------------------------------------------------------------
#
# Regeringsrätten (REGR) bytte namn till Högsta förvaltningsdomstolen (HFD)
# den 1 januari 2011. Mark- och miljööverdomstolen (MMOD) ersatte
# Miljööverdomstolen (MOD) den 2 maj 2011. De historiska och moderna namnen
# avser samma domstol och ska alltid sökas tillsammans.
#
# Nyckeln är det kod användaren anger; värdet är den expanderade listan.

_DOMSTOL_ALIAS: dict[str, list[str]] = {
    "HFD":  ["HFD", "REGR"],
    "REGR": ["HFD", "REGR"],
    "MMOD": ["MMOD", "MOD"],
    "MOD":  ["MMOD", "MOD"],
}


def _expandera_domstolkoder(koder: list[str] | None) -> list[str] | None:
    """
    Expanderar domstolskoder med historiska alias.
    HFD → [HFD, REGR], MMOD → [MMOD, MOD] (och omvänt).
    Bevarar övriga koder oförändrade och tar bort dubbletter.
    """
    if not koder:
        return koder
    expanderade: list[str] = []
    for kod in koder:
        for utokad in _DOMSTOL_ALIAS.get(kod.upper(), [kod.upper()]):
            if utokad not in expanderade:
                expanderade.append(utokad)
    return expanderade


# ---------------------------------------------------------------------------
# Textutdrag och trunkering
# ---------------------------------------------------------------------------

def _tal(n: int) -> str:
    """Heltal med svensk tusentalsavgränsare (hårt blanksteg, U+00A0)."""
    return f"{n:,}".replace(",", " ")


def _skar_ut_text(
    text: str,
    max_tecken: int,
    fran_tecken: int = 0,
    anvisning: Callable[[int], str] | None = None,
    html: bool = False,
) -> str:
    """
    Skär ut ett textutdrag och markera alltid när något kapats.

    Trunkering utan markör är ett tyst datafel — svaret ser ut att vara hela
    innehållet, och ett domskäl som klipps mitt i går inte att skilja från ett
    som slutar där. Ett kapat utdrag avslutas därför med en rad som anger hur
    mycket som visas av hur mycket, och hur resten hämtas.

    max_tecken <= 0 betyder ingen trunkering. Klipper på ord- eller radgräns.

    `anvisning` får utdragets faktiska slutposition och returnerar raden om hur
    man läser vidare. Positionen måste komma härifrån: kapningen på ordgräns gör
    utdraget kortare än max_tecken, och en fortsättning vid fran_tecken +
    max_tecken skulle hoppa över det avkapade ordet. Det sista utdraget får
    ingen läs vidare-rad.

    Med html=True kapas texten aldrig inne i en tagg: hamnar gränsen efter
    ett '<' utan avslutande '>' flyttas den till före taggen.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    kapad = bool(max_tecken and max_tecken > 0 and len(rest) > max_tecken)
    if kapad:
        utdrag    = rest[:max_tecken]
        if html and utdrag.rfind("<") > utdrag.rfind(">"):
            brytpunkt = utdrag.rfind("<")
        else:
            brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        # Ett utdrag som bara består av blanktecken skulle ge slut == start,
        # och läs vidare-raden skulle då peka på samma ställe igen.
        utdrag = utdrag.rstrip() or rest[:max_tecken]
    else:
        utdrag = rest

    if not kapad and start == 0:
        return utdrag

    slut  = start + len(utdrag)
    noter = [f"Visar tecken {_tal(start + 1)}–{_tal(slut)} av {_tal(totalt)}"]
    if kapad and anvisning is not None:
        noter.append(anvisning(slut))
    return utdrag + "\n\n[" + ". ".join(noter) + "]"


# ---------------------------------------------------------------------------
# PDF-extraktion
# ---------------------------------------------------------------------------
#
# PyMuPDF är inte trådsäkert, och verktygen körs på arbetstrådar. Låset
# serialiserar extraktionen. Det skyddar också _tysta_fd1, som flyttar
# processens gemensamma filhandtag: två samtidiga omdirigeringar skulle
# återställa handtagen i fel ordning.

_pdf_las = threading.Lock()


@contextlib.contextmanager
def _tysta_fd1():
    """
    Redirigerar FD 1 och FD 2 till loggfil under anrop som skriver direkt
    till filhandtagen (pymupdf4llm och dess C-bindningar).

    MCP-protokollet skyddas redan av SDK:ns stdio-transport, som läser och
    skriver på egna kopior av handtagen. Omdirigeringen håller i stället
    extraktionens utskrifter borta från stderr, som klienten loggar.
    Anropas bara med _pdf_las taget.
    """
    logg = _LOGS_DIR / "subprocess.log"
    spara_ut = os.dup(1)
    spara_fel = os.dup(2)
    fd = os.open(str(logg), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        yield
    finally:
        os.dup2(spara_ut, 1)
        os.dup2(spara_fel, 2)
        os.close(spara_ut)
        os.close(spara_fel)
        os.close(fd)


# ---------------------------------------------------------------------------
# Anrop mot källan
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _kalla_som_toolerror():
    """Visar källans fel som ToolError, så att klienten får isError och orsaken."""
    try:
        yield
    except klient.KallaFel as e:
        raise ToolError(str(e)) from e


def _sok_post(body: dict) -> dict:
    """Anropar POST /api/v1/sok och returnerar svaret som dict."""
    with _kalla_som_toolerror():
        return klient.sok(body)


def _hamta_publicering_api(avgorande_id: str) -> dict:
    """GET /api/v1/publiceringar/{id} — returnerar fullständigt avgörande."""
    with _kalla_som_toolerror():
        return klient.hamta_publicering(avgorande_id)


def _satt_datumintervall(filter_: dict, datum_fran: str | None, datum_till: str | None) -> None:
    """
    Lägger in avgörandedatumets intervall i sökfiltret.

    API:et läser datumgränserna ur filter.intervall. Lagda direkt i filter
    ignoreras de utan felmeddelande, och sökningen blir då ofiltrerad.
    """
    intervall = {}
    if datum_fran:
        intervall["fromDatum"] = datum_fran
    if datum_till:
        intervall["toDatum"] = datum_till
    if intervall:
        filter_["intervall"] = intervall


def _satt_vagledande(filter_: dict, ar_vagledande: bool | None) -> None:
    """API:et saknar filter på "vägledande"; det uttrycks som avgörandetyper."""
    if ar_vagledande is not None:
        filter_["avgorandeTypLista"] = (
            db.VAGLEDANDE_TYPER if ar_vagledande else db.EJ_VAGLEDANDE_TYPER
        )


# ---------------------------------------------------------------------------
# Intern hjälpfunktion: grupp-kompanjon (DOM_ELLER_BESLUT ↔ REFERAT)
# ---------------------------------------------------------------------------

def _hamta_grupp_kompanjon(avgorande: dict) -> dict | None:
    """
    Hämtar syskonpubliceringen i samma grupp via GET /publiceringar/grupp/{id}.

    gruppKorrelationsnummer grupperar varianterna av samma avgörande:
      DOM_ELLER_BESLUT — publiceras direkt, saknar NJA-nummer
      REFERAT          — publiceras 6–12 mån senare, bär NJA-nummer och rubrik
    En grupp kan också innehålla en notis eller ett beslut om
    prövningstillstånd. Kompanjonen är i första hand motsvarigheten i paret
    dom–referat, annars den första andra publiceringen i gruppen.

    Returnerar kompanjonen eller None om gruppen saknar andra publiceringar.
    Ett fel hos källan loggas och ger None: kompanjonen är ett tillägg till
    ett avgörande som redan hämtats, och ska inte fälla hela svaret.
    """
    grupp_id = avgorande.get("gruppKorrelationsnummer")
    if not grupp_id:
        return None
    try:
        gruppen = klient.hamta_grupp(grupp_id)
    except klient.KallaFel as e:
        log.warning("Kunde inte hämta gruppen %s: %s", grupp_id, e)
        return None

    andra = [p for p in gruppen if p.get("id") != avgorande.get("id")]
    if not andra:
        return None
    motsvarighet = {"DOM_ELLER_BESLUT": "REFERAT", "REFERAT": "DOM_ELLER_BESLUT"}
    onskad = motsvarighet.get(avgorande.get("publiceringsform") or "")
    for p in andra:
        if onskad and p.get("publiceringsform") == onskad:
            return p
    return andra[0]


# ---------------------------------------------------------------------------
# Svarstyper
# ---------------------------------------------------------------------------
#
# Fälten speglar API:ets publiceringar. Källan lämnar många fält tomma för
# äldre referat och för beslut om prövningstillstånd, så allt som inte är
# garanterat av API:ets schema är typat som nullbart.

class Bilaga(TypedDict):
    filnamn: str | None
    fillagring_id: str | None


class Avgorande(TypedDict):
    id: str | None
    typ: str | None
    domstol: str | None
    domstolkod: str | None
    avgorandedatum: str | None
    publiceringstid: str | None
    ar_vagledande: bool | None
    benamning: str | None
    sammanfattning: str | None
    malnummer: list[str]
    referat_nummer: list[str]
    nyckelord: list[str]
    rattsomrade: list[str]
    lagrum: list[dict[str, Any]]
    forarbeten: list[str]
    eu_avgoranden: list[str]
    hanvisade: list[dict[str, Any]]
    litteratur: list[dict[str, Any]]
    ecli_nummer: str | None
    grupp_id: str | None
    har_html_fulltext: bool
    bilagor: list[Bilaga]
    innehall_html: NotRequired[str | None]
    innehall_tecken_totalt: NotRequired[int]
    innehall_trunkerad: NotRequired[bool]
    info: NotRequired[str]


class AvgorandeMedKompanjon(Avgorande):
    kompanjon: NotRequired[Avgorande | None]
    sokresultat_antal: NotRequired[int]


class Forfiningsvarde(TypedDict):
    varde: str
    antal: int


class Forfining(TypedDict):
    antal_varden: int
    varden: list[Forfiningsvarde]


class Forfiningar(TypedDict):
    domstolar: Forfining
    sfs_nummer: Forfining
    rattsomraden: Forfining
    nyckelord: Forfining
    avgorandetyper: Forfining
    publiceringsformer: Forfining
    info: str


class Sokresultat(TypedDict):
    total: int
    sida: int
    antal_per_sida: int
    antal_sidor: int
    avgoranden: list[Avgorande]
    forfiningar: NotRequired[Forfiningar]
    info: NotRequired[str]


class Lagrumsresultat(TypedDict):
    sfs_nummer: str
    paragraf_filter: str | None
    antal_treffar: int
    avgoranden: list[Avgorande]


class Domtexttraff(TypedDict):
    kalla: str
    fillagring_id: str | None
    avgorande_id: str | None
    domstolkod: str | None
    avgorandedatum: str | None
    benamning: str | None
    sammanfattning: str | None
    filnamn: NotRequired[str | None]
    relevans: NotRequired[float]
    utdrag: str | None


class Tackning(TypedDict):
    avgoranden_lokalt: int
    pdf_texter_lokalt: int
    heltackande: bool
    senaste_synk: str | None
    synkstatus: str | None
    fullsynk_klar: str | None


class Domtextresultat(TypedDict):
    sokterm: str
    antal_treffar: int
    info: NotRequired[str]
    tackning: NotRequired[Tackning]
    treffar: list[Domtexttraff]


# ---------------------------------------------------------------------------
# Intern hjälpfunktion: formatering
# ---------------------------------------------------------------------------

def _formatera_avgorande(
    a: dict,
    inkludera_innehall: bool = False,
    max_tecken: int = RP_MAX_TECKEN,
    fran_tecken: int = 0,
) -> dict:
    """
    Formaterar ett avgörande-objekt till ett lämpligt MCP-svar.

    HTML-fulltexten kapas vid max_tecken, eftersom svaret skickas både som
    text och som strukturerat innehåll och ett långt referat annars kan
    närma sig protokollets storleksgräns. Ett kapat utdrag avslutas med en
    rad om hur resten läses med hamta_avgorande.
    """
    har_innehall = bool(a.get("innehall"))
    bilagor = a.get("bilagaLista") or []
    domstol = a.get("domstol") or {}

    result = {
        "id": a.get("id"),
        "typ": a.get("typ"),
        "domstol": domstol.get("domstolNamn"),
        "domstolkod": domstol.get("domstolKod"),
        "avgorandedatum": a.get("avgorandedatum"),
        "publiceringstid": a.get("publiceringstid"),
        "ar_vagledande": db.ar_vagledande(a),
        "benamning": a.get("benamning"),
        "sammanfattning": a.get("sammanfattning"),
        "malnummer": a.get("malNummerLista") or [],
        "referat_nummer": a.get("referatNummerLista") or [],
        "nyckelord": a.get("nyckelordLista") or [],
        "rattsomrade": a.get("rattsomradeLista") or [],
        "lagrum": a.get("lagrumLista") or [],
        "forarbeten": a.get("forarbeteLista") or [],
        "eu_avgoranden": a.get("europarattsligaAvgorandenLista") or [],
        "hanvisade": a.get("hanvisadePubliceringarLista") or [],
        "litteratur": a.get("litteraturLista") or [],
        "ecli_nummer": a.get("ecliNummer"),
        "grupp_id": a.get("gruppKorrelationsnummer"),
        "har_html_fulltext": har_innehall,
        "bilagor": [
            {"filnamn": b.get("filnamn"), "fillagring_id": b.get("fillagringId")}
            for b in bilagor
        ],
    }

    if inkludera_innehall and har_innehall:
        innehall = a.get("innehall") or ""
        avgorande_id = a.get("id")

        def _anvisning(slut: int) -> str:
            return (
                f'Läs vidare: hamta_avgorande(avgorande_id="{avgorande_id}", '
                f"max_tecken={max_tecken}, fran_tecken={slut})"
            )

        result["innehall_html"] = _skar_ut_text(
            innehall, max_tecken, fran_tecken, _anvisning, html=True,
        )
        result["innehall_tecken_totalt"] = len(innehall)
        result["innehall_trunkerad"] = bool(
            max_tecken and max_tecken > 0
            and len(innehall) - max(0, fran_tecken) > max_tecken
        )
    elif not har_innehall and bilagor:
        result["info"] = (
            "Fulltext saknas i API:et för denna domstol. "
            "Använd hamta_pdf med fillagring_id från bilagor[] för att hämta PDF-text."
        )

    return result


# ---------------------------------------------------------------------------
# Sökförfiningar
# ---------------------------------------------------------------------------
#
# /sokforfiningar räknar träffarna per värde för sex dimensioner. Nyckelord
# och lagrum kan ha tusentals olika värden för en bred sökning, så bara de
# vanligaste redovisas; antal_varden visar hur många som finns totalt.

_MAX_FORFININGSVARDEN = 20

# Svarets nycklar hos API:et → namn i verktygets svar
_FORFININGAR = {
    "domstolsidMap": "domstolar",
    "sfsnummerMap": "sfs_nummer",
    "rattsomradeMap": "rattsomraden",
    "sokordMap": "nyckelord",
    "avgorandetypMap": "avgorandetyper",
    "publiceringsformMap": "publiceringsformer",
}


def _formatera_forfiningar(data: dict) -> dict:
    """Sorterar varje dimension efter antal och behåller de vanligaste värdena."""
    resultat: dict = {}
    for api_nyckel, namn in _FORFININGAR.items():
        karta = data.get(api_nyckel) or {}
        sorterade = sorted(karta.items(), key=lambda kv: (-(kv[1] or 0), kv[0]))
        resultat[namn] = {
            "antal_varden": len(karta),
            "varden": [
                {"varde": varde, "antal": int(antal or 0)}
                for varde, antal in sorterade[:_MAX_FORFININGSVARDEN]
            ],
        }
    resultat["info"] = (
        f"Högst {_MAX_FORFININGSVARDEN} värden per dimension, vanligast först. "
        "Snäva in med domstolkoder, sfs_nummer, rattsomrade eller nyckelord. "
        "Domstolarna räknas som om domstolsfiltret inte vore satt, så att "
        "alternativen syns även efter en avgränsning. Källan redovisar inte "
        "fördelningen per år; avgränsa i tid med datum_fran och datum_till."
    )
    return resultat


# ---------------------------------------------------------------------------
# MCP-server
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "rattspraxis",
    instructions=(
        "MCP-server för Domstolsverkets rättspraxis-databas: vägledande avgöranden "
        "från Högsta domstolen, Högsta förvaltningsdomstolen, Arbetsdomstolen, "
        "Mark- och miljööverdomstolen, Migrationsöverdomstolen, hovrätterna och "
        "kammarrätterna. Rättsfallsreferat finns från 1981; domar och beslut i "
        "fulltext från mars 2025.\n\n"
        "VAR DU BÖRJAR: en känd beteckning (NJA 2025:67, HFD 2026 ref. 1, "
        "målnummer, HD:s kortnamn) -> hamta_avgorande_pa_beteckning. Praxis om en "
        "viss paragraf -> sok_rattpraxis_for_lagrum. Allt annat -> sok_rattpraxis, "
        "sedan hamta_avgorande med id-värdet ur träffen.\n\n"
        "FULLTEXT: HTML-fulltext finns för de flesta domstolar och för alla "
        "referat. Domar och beslut från HD och MÖD saknar HTML; läs dem med "
        "hamta_pdf och fillagring_id ur bilagor[]. En kapad domtext avslutas med "
        "en rad som anger hur resten hämtas — citera aldrig ur ett kapat utdrag.\n\n"
        "SÖKNING I DOMTEXTEN: sok_i_domtext söker i den lokala databasen, inte "
        "hos källan. Efter en fullsynk täcker den hela korpusen; fältet "
        "tackning i svaret visar hur mycket som finns lokalt. En bred sökning "
        "hos källan snävas in med sok_rattpraxis(forfiningar=true)."
    ),
    version="1.2.0",
    cache_hints=CACHE_HINTAR,
)


# ---------------------------------------------------------------------------
# Verktyg
# ---------------------------------------------------------------------------

_BESKR_AR_VAGLEDANDE = (
    "true = bara prejudikat och vägledande avgöranden; false = bara avgöranden "
    "som inte är vägledande och beslut om prövningstillstånd"
)


@mcp.tool(title="Sök i rättspraxis", annotations=LASNING_EXTERN)
def sok_rattpraxis(
    fritext: Annotated[str | None, Field(
        description="Fritextsökning — ord AND-kombineras. Exempel: 'skadestånd entreprenad'",
    )] = None,
    domstolkoder: Annotated[list[str] | None, Field(
        description=(
            "Filtrera på domstol. Koder: HDO (HD), HFD, ADO (AD), "
            "MMOD (MÖD), MIOD (Migrationsöverdomstolen), HSV (Svea hovrätt) m.fl. "
            "HFD och REGR (Regeringsrätten, t.o.m. 2010) expanderas automatiskt "
            "till båda — ange endera för att söka hela beståndet. "
            "Detsamma gäller MMOD och MOD (Miljööverdomstolen, t.o.m. 2011)."
        ),
    )] = None,
    ar_vagledande: Annotated[bool | None, Field(description=_BESKR_AR_VAGLEDANDE)] = None,
    rattsomrade: Annotated[str | None, Field(
        description=(
            "Rättsområde. Alternativ: Miljömål, Skatt, Migrationsmål, "
            "Brottmål inkl mål om utdömande av vite, Socialförsäkring m.fl."
        ),
    )] = None,
    sfs_nummer: Annotated[str | None, Field(
        description="SFS-nummer för lag, t.ex. '1942:740' (rättegångsbalken)",
    )] = None,
    datum_fran: Annotated[str | None, Field(description="Från-datum ÅÅÅÅ-MM-DD")] = None,
    datum_till: Annotated[str | None, Field(description="Till-datum ÅÅÅÅ-MM-DD")] = None,
    nyckelord: Annotated[str | None, Field(
        description="Ämnesord från avgörandenas nyckelordslista",
    )] = None,
    sid_index: Annotated[int, Field(description="Sidindex, 0-baserat (standard: 0)")] = 0,
    antal_per_sida: Annotated[int, Field(
        description="Träffar per sida, 1–50 (standard: 10)",
    )] = 10,
    forfiningar: Annotated[bool, Field(
        description=(
            "true = redovisa även hur träffarna fördelar sig på domstolar, lagar "
            "(SFS-nummer), rättsområden, nyckelord, avgörandetyper och "
            "publiceringsformer, med antal per värde. Använd vid breda sökningar "
            "för att se hur de kan snävas in (standard: false)"
        ),
    )] = False,
) -> Sokresultat:
    """
    Söker i Domstolsverkets rättspraxis-databas (~17 500 avgöranden från svenska
    överrätter). Rättsfallsreferat finns från 1981; domar och beslut i fulltext
    finns från mars 2025. Returnerar sammanfattningar, lagrumshänvisningar och
    korsreferenser till förarbeten och EU-domstolsbeslut. Använd sfs_nummer för
    praxis kopplad till en specifik lag, domstolkoder=['HDO'] för enbart Högsta
    domstolens prejudikat. Ger sökningen många träffar: sätt forfiningar=true
    för att se hur de fördelar sig på domstolar, lagar och nyckelord.
    """
    antal_per_sida = max(1, min(int(antal_per_sida or 10), _MAX_PER_SIDA))
    sid_index = max(0, int(sid_index or 0))

    body = {
        "sokfras": {
            "andLista": [w.strip() for w in fritext.split() if w.strip()] if fritext else [],
            "exaktFras": None,
        },
        "filter": {},
        "sortorder": "desc",
        "sidIndex": sid_index,
        "antalPerSida": antal_per_sida,
    }

    f = body["filter"]
    if domstolkoder:
        f["domstolKodLista"] = _expandera_domstolkoder(domstolkoder)
    _satt_vagledande(f, ar_vagledande)
    if rattsomrade:
        f["rattsomradeLista"] = [rattsomrade]
    if sfs_nummer:
        f["sfsNummerLista"] = [sfs_nummer]
    _satt_datumintervall(f, datum_fran, datum_till)
    if nyckelord:
        f["sokordLista"] = [nyckelord]

    data = _sok_post(body)
    total = data.get("total") or 0
    treffar = data.get("publiceringLista") or []

    antal_sidor = (total + antal_per_sida - 1) // antal_per_sida if total > 0 else 0
    resultat: Sokresultat = {
        "total": total,
        "sida": sid_index + 1,
        "antal_per_sida": antal_per_sida,
        "antal_sidor": antal_sidor,
        "avgoranden": [_formatera_avgorande(a) for a in treffar],
    }

    if forfiningar:
        # Förfiningarna är ett tillägg till träffarna. Svarar källan inte på
        # dem redovisas träffarna ändå, med en notering om vad som saknas.
        try:
            resultat["forfiningar"] = _formatera_forfiningar(klient.sokforfiningar(body))
        except klient.KallaFel as e:
            log.warning("sokforfiningar misslyckades: %s", e)
            resultat["info"] = f"Förfiningarna kunde inte hämtas: {e}"
    return resultat


@mcp.tool(title="Hämta avgörande", annotations=LASNING_EXTERN)
def hamta_avgorande(
    avgorande_id: Annotated[str, Field(
        description="Avgörandets UUID (avgorande_id från sok_rattpraxis)",
    )],
    inkludera_html: Annotated[bool, Field(
        description="Inkludera HTML-fulltext i svaret om tillgänglig (standard: true)",
    )] = True,
    hamta_kompanjon: Annotated[bool, Field(
        description=(
            "Hämta även syskonpublicering (DOM_ELLER_BESLUT↔REFERAT med NJA-nummer) "
            "om tillgänglig (standard: false)"
        ),
    )] = False,
    max_tecken: Annotated[int, Field(
        description=(
            "Teckentak för HTML-fulltexten (standard 60 000, 0 = hela texten). "
            "En kapad text avslutas med en rad som anger hur mycket som visas "
            "och hur resten hämtas; innehall_trunkerad är då true."
        ),
    )] = RP_MAX_TECKEN,
    fran_tecken: Annotated[int, Field(
        description=(
            "Börja HTML-fulltexten vid denna teckenposition — för att läsa "
            "vidare där ett kapat svar slutade. Citera aldrig ur ett kapat utdrag."
        ),
    )] = 0,
) -> AvgorandeMedKompanjon:
    """
    Hämtar ett fullständigt avgörande med all metadata: lagrum,
    förarbeteshänvisningar, EU-rättshänvisningar, nyckelord och fulltext (HTML)
    om tillgänglig. HTML-fulltext finns för HFD m.fl. men saknas för HD (HDO)
    och MÖD (MMOD) — använd hamta_pdf för dessa.
    """
    a = _las_avgorande_cache(avgorande_id)
    kalla = "cache"

    if a is None:
        a = _hamta_publicering_api(avgorande_id)
        _skriv_avgorande_cache(a)
        kalla = "api"

    log.info("hamta_avgorande %s — källa: %s", avgorande_id, kalla)
    result = _formatera_avgorande(
        a, inkludera_innehall=bool(inkludera_html),
        max_tecken=max_tecken, fran_tecken=fran_tecken,
    )

    if hamta_kompanjon:
        kompanjon = _hamta_grupp_kompanjon(a)
        result["kompanjon"] = _formatera_avgorande(kompanjon) if kompanjon else None

    return result


def _sakerstall_avgorande_cache(avgorande_id: str | None) -> None:
    """
    Säkerställer att metadata för ett avgörande finns i avgorande_cache.
    Hämtar från API:et om cachen är tom eller utgången.
    Anropas från hamta_pdf så att sok_i_domtext alltid kan returnera
    domstolkod, avgorandedatum och benamning — även när hamta_avgorande
    aldrig anropats direkt.
    """
    if not avgorande_id:
        return
    if _las_avgorande_cache(avgorande_id) is not None:
        return
    try:
        a = klient.hamta_publicering(avgorande_id)
        _skriv_avgorande_cache(a)
        log.info("Metadata cachad för avgörande %s (via hamta_pdf)", avgorande_id)
    except Exception as e:
        log.warning("Kunde inte cacha metadata för avgörande %s: %s", avgorande_id, e)


@mcp.tool(title="Hämta domtext ur PDF", annotations=LASNING_EXTERN, structured_output=False)
def hamta_pdf(
    fillagring_id: Annotated[str, Field(
        description=(
            "Fillagrings-ID från bilagor[].fillagring_id, t.ex. '190/b6/74/uuid'. "
            "Snedstrecken URL-kodas automatiskt."
        ),
    )],
    avgorande_id: Annotated[str | None, Field(
        description=(
            "UUID för avgörandet — används för att koppla PDF till avgörande i "
            "cachen (valfritt)"
        ),
    )] = None,
    filnamn: Annotated[str | None, Field(
        description="Filnamn på PDF:en, t.ex. 'B 712-25.pdf' (valfritt, för cachelogg)",
    )] = None,
    max_tecken: Annotated[int, Field(
        description=(
            "Teckentak för den returnerade domtexten (standard 60 000, 0 = hela texten). "
            "Sätt ett tak för långa domar så att svaret inte överskrider "
            "storleksgränsen. Ett kapat svar avslutas med en rad som anger "
            "hur mycket som visas och hur resten hämtas."
        ),
    )] = RP_MAX_TECKEN,
    fran_tecken: Annotated[int, Field(
        description=(
            "Börja texten vid denna teckenposition — för att läsa vidare "
            "där ett kapat svar slutade. Citera aldrig ur ett kapat utdrag."
        ),
    )] = 0,
) -> str:
    """
    Hämtar och extraherar text ur PDF-bilagan till ett avgörande. Nödvändigt för
    HD (HDO) och MÖD (MMOD) som saknar HTML-fulltext i API:et. Extraherad text
    cachas lokalt — efterföljande anrop hämtar från cache. fillagring_id hämtas
    från bilagor[].fillagring_id i svaret från hamta_avgorande.
    """
    def _anvisning(slut: int) -> str:
        # Ett komplett anrop: samma pdf, samma tak, från utdragets faktiska slut.
        argument = [f'fillagring_id="{fillagring_id}"']
        if avgorande_id:
            argument.append(f'avgorande_id="{avgorande_id}"')
        argument += [f"max_tecken={max_tecken}", f"fran_tecken={slut}"]
        return f"Läs vidare: hamta_pdf({', '.join(argument)})"

    cachad_text = _las_pdf_cache(fillagring_id)
    if cachad_text:
        log.info("hamta_pdf %s — returnerar från cache", fillagring_id)
        # Retroaktiv metadata-fyllning: säkerställ att avgorande_cache är
        # populerad även för PDF:er som cachades utan metadata.
        _sakerstall_avgorande_cache(avgorande_id)
        return _skar_ut_text(cachad_text, max_tecken, fran_tecken, _anvisning)

    # pymupdf4llm importeras först här — det krävs bara när en PDF hämtas.
    try:
        import fitz
        import pymupdf4llm
    except ImportError as e:
        raise ToolError(
            "pymupdf4llm är inte installerat. Kör: pip install pymupdf4llm "
            "och starta om MCP-servern."
        ) from e

    log.info("Hämtar PDF: %s", fillagring_id)
    with _kalla_som_toolerror():
        pdf_bytes = klient.hamta_bilaga(fillagring_id)

    try:
        with _pdf_las:
            dok = fitz.open(stream=pdf_bytes, filetype="pdf")
            try:
                with _tysta_fd1():
                    markdown_text = pymupdf4llm.to_markdown(dok)
            finally:
                dok.close()
        log.info("PDF extraherad: %d tecken, %d bytes", len(markdown_text), len(pdf_bytes))
    except Exception as e:
        log.error("Fel vid PDF-extraktion av %s: %s", fillagring_id, e)
        raise ToolError(
            f"Texten i PDF:en '{fillagring_id}' kunde inte extraheras ({e}). "
            "Filen kan vara skadad eller bara innehålla inskannade bilder."
        ) from e

    _skriv_pdf_cache(
        fillagring_id=fillagring_id,
        text_md=markdown_text,
        avgorande_id=avgorande_id,
        filstorlek=len(pdf_bytes),
        filnamn=filnamn,
    )

    # Säkerställ att metadata finns i avgorande_cache så att sok_i_domtext
    # kan returnera domstolkod, avgorandedatum och benamning.
    _sakerstall_avgorande_cache(avgorande_id)

    # Cachen har alltid hela texten — trunkeringen gäller bara svaret.
    return _skar_ut_text(markdown_text, max_tecken, fran_tecken, _anvisning)


@mcp.tool(title="Sök praxis för lagrum", annotations=LASNING_EXTERN)
def sok_rattpraxis_for_lagrum(
    sfs_nummer: Annotated[str, Field(
        description="SFS-nummer, t.ex. '1962:700' (brottsbalken)",
    )],
    paragraf: Annotated[str | None, Field(
        description=(
            "Paragraf att filtrera på, t.ex. '36 §', '5 kap. 3 §'. "
            "Partiell matchning — '36 §' matchar även '36 a §'."
        ),
    )] = None,
    ar_vagledande: Annotated[bool | None, Field(description=_BESKR_AR_VAGLEDANDE)] = None,
    datum_fran: Annotated[str | None, Field(description="Från-datum ÅÅÅÅ-MM-DD")] = None,
    datum_till: Annotated[str | None, Field(description="Till-datum ÅÅÅÅ-MM-DD")] = None,
    max_antal: Annotated[int, Field(
        description="Max antal träffar (standard: 20, max: 200)",
    )] = 20,
) -> Lagrumsresultat:
    """
    Söker rättspraxis kopplad till ett specifikt lagrum i en lag. Mer precist än
    sok_rattpraxis(sfs_nummer=...) eftersom det kan filtrera på specifik
    paragraf, t.ex. '36 §' eller '5 kap. 3 §'. Hämtar alla träffar för
    SFS-numret och filtrerar på paragraf.
    """
    max_antal = max(1, min(int(max_antal or 20), 200))
    para_filter = (paragraf or "").lower().strip()

    body = {
        "sokfras": {"andLista": [], "exaktFras": None},
        "filter": {"sfsNummerLista": [sfs_nummer]},
        "sortorder": "desc",
        "sidIndex": 0,
        "antalPerSida": _MAX_PER_SIDA,
    }

    f = body["filter"]
    _satt_vagledande(f, ar_vagledande)
    _satt_datumintervall(f, datum_fran, datum_till)

    alla: list[dict] = []
    sid = 0

    while len(alla) < max_antal:
        body["sidIndex"] = sid
        data = _sok_post(body)
        treffar = data.get("publiceringLista") or []
        total = data.get("total") or 0

        if not treffar:
            break

        if para_filter:
            for a in treffar:
                for lagrum in a.get("lagrumLista") or []:
                    # Kontrollera att paragrafen tillhör rätt SFS-nummer, inte
                    # ett annat lagrum i samma avgörande med liknande paragrafbeteckning.
                    if (
                        para_filter in (lagrum.get("referens") or "").lower()
                        and lagrum.get("sfsNummer") == sfs_nummer
                    ):
                        alla.append(a)
                        break
        else:
            alla.extend(treffar)

        hamtade_totalt = (sid + 1) * _MAX_PER_SIDA
        if hamtade_totalt >= total:
            break
        sid += 1

    alla = alla[:max_antal]

    return {
        "sfs_nummer": sfs_nummer,
        "paragraf_filter": paragraf,
        "antal_treffar": len(alla),
        "avgoranden": [_formatera_avgorande(a) for a in alla],
    }


@mcp.tool(title="Hämta avgörande på beteckning", annotations=LASNING_EXTERN)
def hamta_avgorande_pa_beteckning(
    beteckning: Annotated[str, Field(
        description=(
            "Referensnummer eller kortnamn. Exempel: 'NJA 2025:67', "
            "'HFD 2026 ref. 1', '\"Ringa stöld-gränsen II\"', 'B 712-25'"
        ),
    )],
    hamta_kompanjon: Annotated[bool, Field(
        description="Hämta syskonpublicering (DOM↔REFERAT) om tillgänglig (standard: true)",
    )] = True,
) -> AvgorandeMedKompanjon:
    """
    Hämtar ett avgörande via referensnummer eller kortnamn. Stöder:
    • NJA-nummer: 'NJA 2025:67' eller 'NJA 2025 s. 1024'
    • HFD-referat: 'HFD 2026 ref. 1'
    • MÖD: 'MÖD 2025:51', AD: 'AD 2024 nr 47'
    • HD:s kortnamn: '"Ringa stöld-gränsen II"'
    • Målnummer: 'B 712-25', 'Ö 6478-25'
    Returnerar avgörandet med kompanjonpublicering (DOM↔REFERAT) om tillgänglig.
    """
    beteckning = (beteckning or "").strip()
    if not beteckning:
        raise ToolError("Ange en beteckning, t.ex. 'NJA 2025:67' eller 'B 712-25'.")

    # Steg 1: exaktFras-sökning (hanterar NJA-nummer, HFD-ref, kortnamn)
    body = {
        "sokfras": {"andLista": [], "exaktFras": beteckning},
        "filter": {},
        "sortorder": "desc",
        "sidIndex": 0,
        "antalPerSida": 10,
    }
    data = _sok_post(body)
    treffar = data.get("publiceringLista") or []

    # Steg 2: AND-sökning som reservväg
    if not treffar:
        ord_lista = [w for w in beteckning.replace('"', "").split() if len(w) > 2]
        if ord_lista:
            body["sokfras"] = {"andLista": ord_lista, "exaktFras": None}
            data = _sok_post(body)
            treffar = data.get("publiceringLista") or []

    if not treffar:
        raise ToolError(
            f"Inga avgöranden hittades för '{beteckning}'. Kontrollera beteckningen, "
            "eller sök bredare med sok_rattpraxis(fritext=...)."
        )

    huvud = treffar[0]

    # Hämta fullständigt avgörande (via cache eller API)
    a = _las_avgorande_cache(huvud["id"])
    if a is None:
        try:
            a = klient.hamta_publicering(huvud["id"])
            _skriv_avgorande_cache(a)
        except klient.KallaFel as e:
            log.warning("Kunde inte hämta hela avgörandet %s: %s", huvud["id"], e)
            a = huvud

    result = _formatera_avgorande(a, inkludera_innehall=True)
    result["sokresultat_antal"] = len(treffar)

    if hamta_kompanjon:
        kompanjon = None
        grupp = a.get("gruppKorrelationsnummer")

        # Kolla om kompanjonen redan finns bland sökträffarna
        if grupp:
            for t in treffar[1:]:
                if (
                    t.get("gruppKorrelationsnummer") == grupp
                    and t.get("id") != a.get("id")
                ):
                    kompanjon = t
                    break

        # Annars hämta gruppen
        if kompanjon is None:
            kompanjon = _hamta_grupp_kompanjon(a)

        result["kompanjon"] = _formatera_avgorande(kompanjon) if kompanjon else None

    return result


def _tackning() -> dict | None:
    """Hur mycket av korpusen som finns lokalt. None om det inte går att läsa."""
    try:
        antal = db.rakna_lokalt()
        lage = db.las_synk_status("publiceringar") or {}
    except Exception as e:
        log.warning("Täckningen kunde inte läsas: %s", e)
        return None
    senaste = lage.get("avslutad")
    aktuell = False
    if senaste:
        try:
            tid = datetime.fromisoformat(str(senaste))
            if tid.tzinfo is None:
                tid = tid.replace(tzinfo=timezone.utc)
            aktuell = datetime.now(timezone.utc) - tid <= timedelta(days=RP_TACKNING_MAX_DAGAR)
        except ValueError:
            log.warning("Oläsligt synkdatum i synk_status: %r", senaste)
    return {
        "avgoranden_lokalt": antal["avgoranden"],
        "pdf_texter_lokalt": antal["pdf_texter"],
        "heltackande": bool(lage.get("fullsynk_klar")) and aktuell,
        "senaste_synk": senaste,
        "synkstatus": lage.get("status"),
        "fullsynk_klar": lage.get("fullsynk_klar"),
    }


def _sok_domtext_postgres(cur, sokterm: str, domstolkoder: list[str] | None,
                          antal: int) -> list[dict]:
    """Fulltextsökning med relevansrankning och utdrag i båda textkällorna."""
    domstol_filter = ""
    domstol_params: list = []
    if domstolkoder:
        domstol_filter = f"AND ac.domstolkod IN ({', '.join(['%s'] * len(domstolkoder))})"
        domstol_params = list(domstolkoder)

    # ts_headline är dyr på långa domar, så utdragen tas bara fram för de
    # högst rankade träffarna i den yttre frågan.
    fraga = f"""
        WITH q AS (SELECT plainto_tsquery('swedish', %s) AS q),
        traffar AS (
            SELECT 'pdf' AS kalla, pc.fillagring_id, pc.avgorande_id,
                   ac.domstolkod, ac.avgorandedatum, ac.benamning,
                   ac.data->>'sammanfattning' AS sammanfattning, pc.filnamn,
                   ts_rank(pc.text_tsv, q.q) AS rank, pc.text_md AS text
            FROM rattspraxis.pdf_cache pc
            CROSS JOIN q
            LEFT JOIN rattspraxis.avgorande_cache ac ON ac.id = pc.avgorande_id
            WHERE pc.text_tsv @@ q.q {domstol_filter}
            UNION ALL
            SELECT 'avgorande', NULL, ac.id,
                   ac.domstolkod, ac.avgorandedatum, ac.benamning,
                   ac.data->>'sammanfattning', NULL,
                   ts_rank(ac.sokbar_tsv, q.q), ac.sokbar_text
            FROM rattspraxis.avgorande_cache ac
            CROSS JOIN q
            WHERE ac.sokbar_tsv @@ q.q {domstol_filter}
            ORDER BY rank DESC
            LIMIT %s
        )
        SELECT t.kalla, t.fillagring_id, t.avgorande_id, t.domstolkod,
               t.avgorandedatum, t.benamning, t.sammanfattning, t.filnamn, t.rank,
               ts_headline('swedish', t.text, q.q,
                   'MaxFragments=3, MaxWords=40, MinWords=15,
                    StartSel=>>>, StopSel=<<<')
        FROM traffar t CROSS JOIN q
        ORDER BY t.rank DESC
    """
    cur.execute(fraga, [sokterm, *domstol_params, *domstol_params, antal])
    return [
        {
            "kalla": rad[0],
            "fillagring_id": rad[1],
            "avgorande_id": rad[2],
            "domstolkod": rad[3],
            "avgorandedatum": str(rad[4]) if rad[4] else None,
            "benamning": rad[5],
            "sammanfattning": rad[6],
            "filnamn": rad[7],
            "relevans": float(rad[8]) if rad[8] else 0.0,
            "utdrag": rad[9],
        }
        for rad in cur.fetchall()
    ]


def _sok_domtext_sqlite(cur, sokterm: str, domstolkoder: list[str] | None,
                        antal: int) -> list[dict]:
    """Enkel delsträngssökning (LIKE) i båda textkällorna, utan rankning."""
    domstol_filter = ""
    domstol_params: list = []
    if domstolkoder:
        domstol_filter = f"AND ac.domstolkod IN ({', '.join(['?'] * len(domstolkoder))})"
        domstol_params = list(domstolkoder)
    monster = f"%{sokterm}%"
    cur.execute(f"""
        SELECT 'pdf', pc.fillagring_id, pc.avgorande_id, ac.domstolkod,
               ac.avgorandedatum, ac.benamning,
               json_extract(ac.data, '$.sammanfattning'), pc.filnamn,
               substr(pc.text_md, max(instr(lower(pc.text_md), lower(?)) - 100, 1), 300)
        FROM pdf_cache pc
        LEFT JOIN avgorande_cache ac ON ac.id = pc.avgorande_id
        WHERE lower(pc.text_md) LIKE lower(?) {domstol_filter}
        UNION ALL
        SELECT 'avgorande', NULL, ac.id, ac.domstolkod,
               ac.avgorandedatum, ac.benamning,
               json_extract(ac.data, '$.sammanfattning'), NULL,
               substr(ac.sokbar_text, max(instr(lower(ac.sokbar_text), lower(?)) - 100, 1), 300)
        FROM avgorande_cache ac
        WHERE lower(ac.sokbar_text) LIKE lower(?) {domstol_filter}
        LIMIT ?
    """, [sokterm, monster, *domstol_params, sokterm, monster, *domstol_params, antal])
    return [
        {
            "kalla": rad[0],
            "fillagring_id": rad[1],
            "avgorande_id": rad[2],
            "domstolkod": rad[3],
            "avgorandedatum": rad[4],
            "benamning": rad[5],
            "sammanfattning": rad[6],
            "filnamn": rad[7],
            "utdrag": rad[8],
        }
        for rad in cur.fetchall()
    ]


@mcp.tool(title="Sök i lokalt lagrade domtexter", annotations=LASNING_DB)
def sok_i_domtext(
    sokterm: Annotated[str, Field(
        description="Sökterm eller fras att leta efter i domtexterna",
    )],
    domstolkod: Annotated[str | None, Field(
        description="Filtrera på domstol: HDO, HFD, MMOD m.fl. (valfritt)",
    )] = None,
    max_antal: Annotated[int, Field(
        description="Max antal träffar (standard: 10, max: 50)",
    )] = 10,
) -> Domtextresultat:
    """
    Söker fulltext inuti domstolsavgöranden i den lokala databasen: HTML-
    fulltexten, sammanfattningen och benämningen för varje lokalt lagrat
    avgörande, samt PDF-texter som hämtats med hamta_pdf. När servern synkas
    dagligen omfattar databasen hela Domstolsverkets korpus; annars bara de
    avgöranden som hämtats tidigare. Fältet tackning visar vilket. Domar och
    beslut från HD och MÖD som bara finns som PDF är sökbara på
    sammanfattningen tills PDF:en hämtats. Använd för att hitta specifika
    resonemang, lagcitat eller rättsliga principer i domtexterna — kompletterar
    metadata-sökning med sökning i domskälen. Varje träff anger kalla
    ('avgorande' eller 'pdf'). PostgreSQL: avancerad FTS med träffrelevans och
    utdrag. SQLite: enklare textsökning.
    """
    max_antal = max(1, min(int(max_antal or 10), 50))
    # Expandera domstolkod till historiska alias (HFD↔REGR, MMOD↔MOD)
    domstolkoder = _expandera_domstolkoder([domstolkod]) if domstolkod else None

    if not _DATABASE_URL:
        raise ToolError(
            "sok_i_domtext kräver en databas. Ange DATABASE_URL i .env "
            "(PostgreSQL eller SQLite) och starta om servern."
        )

    try:
        conn = _hamta_db()
    except Exception as e:
        log.error("sok_i_domtext: databasen gick inte att öppna: %s", e)
        raise ToolError(
            "Databasen i DATABASE_URL gick inte att nå. Kontrollera att "
            "databasservern är igång och försök igen."
        ) from e

    # Samma avgörande kan träffa både i sin HTML-text och i sin PDF. Det
    # hämtas därför fler rader än som visas, och varje avgörande redovisas
    # en gång, med sin bäst rankade träff.
    try:
        cur = conn.cursor()
        if _ar_postgres():
            rader = _sok_domtext_postgres(cur, sokterm, domstolkoder, max_antal * 2)
        else:
            rader = _sok_domtext_sqlite(cur, sokterm, domstolkoder, max_antal * 2)
    except Exception as e:
        log.error("Fel i sok_i_domtext: %s", e, exc_info=True)
        raise ToolError(f"Sökningen i den lokala databasen misslyckades: {e}") from e
    finally:
        conn.close()

    treffar: list[dict] = []
    sedda: set[str] = set()
    for rad in rader:
        nyckel = rad["avgorande_id"] or rad["fillagring_id"] or ""
        if nyckel in sedda:
            continue
        sedda.add(nyckel)
        treffar.append(rad)
        if len(treffar) >= max_antal:
            break

    resultat: Domtextresultat = {
        "sokterm": sokterm,
        "antal_treffar": len(treffar),
        "treffar": treffar,
    }
    tackning = _tackning()
    if tackning is not None:
        resultat["tackning"] = tackning
        if not tackning["heltackande"]:
            if tackning["fullsynk_klar"]:
                orsak = (
                    f"Senaste lyckade synk var {tackning['senaste_synk']}, mer än "
                    f"{RP_TACKNING_MAX_DAGAR} dagar sedan; senare publiceringar "
                    "saknas lokalt. Kontrollera att den dagliga synken körs "
                    "(logs/synk-*.log)."
                )
            else:
                orsak = (
                    "Ingen fullsynk har gjorts. Kör 01_synka_publiceringar.py för "
                    "att lagra alla publiceringar lokalt."
                )
            resultat["info"] = (
                f"Sökningen omfattar de {tackning['avgoranden_lokalt']} avgöranden "
                "och de PDF-texter som finns lokalt, inte med säkerhet hela "
                f"Domstolsverkets korpus. {orsak}"
            )
    return resultat


# ---------------------------------------------------------------------------
# Uppstart
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    starta(mcp, standardport=8005, initiera=_sakerstall_schema)
