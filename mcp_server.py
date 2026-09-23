#!/usr/bin/env python3
"""
MCP-server för Domstolsverkets rättspraxis-API.

Exponerar sex verktyg:
  sok_rattpraxis                 — fritextsökning med filter
  hamta_avgorande                — hämta fullständigt avgörande på ID
  hamta_pdf                      — hämta + extrahera PDF-bilaga (cachas lokalt)
  sok_rattpraxis_for_lagrum      — sök på specifik paragraf i en lag
  hamta_avgorande_pa_beteckning  — sök på NJA-nummer, HFD-referat, kortnamn, målnummer
  sok_i_domtext                  — fulltext-sökning inuti cachade domar (FTS)

Konfiguration via .env-fil — se config.example.env.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

import db
from db import (
    DATABASE_URL as _DATABASE_URL,
    _ar_postgres,
    _hamta_db,
    _sakerstall_schema,
    _las_avgorande_cache,
    _skriv_avgorande_cache,
    _las_pdf_cache,
    _skriv_pdf_cache,
)

# ---------------------------------------------------------------------------
# Inledande inställningar
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

_LOGS_DIR = _SCRIPT_DIR / "logs"
_LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(_LOGS_DIR / "mcp_server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

_API_BAS = "https://rattspraxis.etjanst.domstol.se/api/v1"

# Standardtak för domtext i hamta_pdf. Utan ett tak som gäller by default kan ett
# långt avgörande överskrida MCP-protokollets storleksgräns och misslyckas helt.
# Anroparen kan alltid höja taket, eller sätta 0 för hela texten.
RP_MAX_TECKEN = int(os.getenv("RP_MAX_TECKEN", "60000"))

# Maximalt antal träffar per API-sida — API:et tillåter upp till 50.
# Värdet är ett designval: 50 balanserar svarstid mot täckning vid paginering.
_MAX_PER_SIDA = 50

# Projektidentifierande UA enligt projektets UA-konvention. Skickas med på
# alla anrop mot Domstolsverkets rättspraxis-API.
_HEADERS = {
    "User-Agent": "mcp-for-rattspraxis/1.0 (+https://github.com/MagnusKolsjo/mcp-for-rattspraxis)",
}

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
    return f"{n:,}".replace(",", " ")


def _skar_ut_text(
    text: str,
    max_tecken: int,
    fran_tecken: int = 0,
    anvisning: str = "",
) -> str:
    """
    Skär ut ett textutdrag och markera alltid när något kapats.

    Trunkering utan markör är ett tyst datafel — svaret ser ut att vara hela
    innehållet, och ett domskäl som klipps mitt i går inte att skilja från ett
    som slutar där. Ett kapat utdrag avslutas därför med en rad som anger hur
    mycket som visas av hur mycket, och hur resten hämtas.

    max_tecken <= 0 betyder ingen trunkering. Klipper på ord- eller radgräns.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    kapad = bool(max_tecken and max_tecken > 0 and len(rest) > max_tecken)
    if kapad:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        utdrag = utdrag.rstrip()
    else:
        utdrag = rest

    if not kapad and start == 0:
        return utdrag

    slut  = start + len(utdrag)
    noter = [f"Visar tecken {_tal(start + 1)}–{_tal(slut)} av {_tal(totalt)}"]
    if anvisning:
        noter.append(anvisning)
    return utdrag + "\n\n[" + ". ".join(noter) + "]"


# ---------------------------------------------------------------------------
# FD 1-skydd — skyddar MCP-protokollet mot C-bindningars stdout-utskrifter
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _tysta_fd1():
    """
    Redirigerar FD 1 och FD 2 till loggfil under anrop som kan skriva
    direkt till filhandtagen (t.ex. pymupdf4llm, Tesseract).
    Återställer FD-handtagen efteråt så att MCP-protokollet fungerar.
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
# HTTP-hjälpfunktioner mot Domstolsverkets API
# ---------------------------------------------------------------------------

def _sok_post(body: dict) -> dict:
    """Anropar POST /api/v1/sok och returnerar svaret som dict."""
    url = f"{_API_BAS}/sok"
    r = requests.post(url, json=body, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def _hamta_publicering_api(avgorande_id: str) -> dict:
    """GET /api/v1/publiceringar/{id} — returnerar fullständigt avgörande."""
    url = f"{_API_BAS}/publiceringar/{avgorande_id}"
    r = requests.get(url, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


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


# ---------------------------------------------------------------------------
# Intern hjälpfunktion: grupp-kompanjon (DOM_ELLER_BESLUT ↔ REFERAT)
# ---------------------------------------------------------------------------

def _hamta_grupp_kompanjon(avgorande: dict) -> dict | None:
    """
    Slår upp syskonpublication via benamning-exaktsökning.

    gruppKorrelationsnummer grupperar två varianter av samma avgörande:
      DOM_ELLER_BESLUT — publiceras direkt, saknar NJA-nummer
      REFERAT          — publiceras 6–12 mån senare, bär NJA-nummer och rubrik

    Returnerar kompanjonen eller None om ingen hittas.
    """
    benamning = (avgorande.get("benamning") or "").strip()
    if not benamning:
        return None

    body = {
        "sokfras": {"andLista": [], "exaktFras": benamning},
        "filter": {},
        "sortorder": "desc",
        "sidIndex": 0,
        "antalPerSida": 5,
    }
    try:
        data = _sok_post(body)
        for hit in data.get("publiceringLista", []):
            if (
                hit.get("id") != avgorande.get("id")
                and (hit.get("benamning") or "").strip() == benamning
            ):
                return hit
    except Exception as e:
        log.warning("Kunde inte hämta grupp-kompanjon för '%s': %s", benamning, e)
    return None


# ---------------------------------------------------------------------------
# Intern hjälpfunktion: formatering
# ---------------------------------------------------------------------------

def _formatera_avgorande(a: dict, inkludera_innehall: bool = False) -> dict:
    """Formaterar ett avgörande-objekt till ett lämpligt MCP-svar."""
    har_innehall = bool(a.get("innehall"))
    bilagor = a.get("bilagaLista", [])

    result = {
        "id": a.get("id"),
        "typ": a.get("typ"),
        "domstol": (a.get("domstol") or {}).get("domstolNamn"),
        "domstolkod": (a.get("domstol") or {}).get("domstolKod"),
        "avgorandedatum": a.get("avgorandedatum"),
        "publiceringstid": a.get("publiceringstid"),
        "ar_vagledande": a.get("arVagledande"),
        "benamning": a.get("benamning"),
        "sammanfattning": a.get("sammanfattning"),
        "malnummer": a.get("malNummerLista", []),
        "referat_nummer": a.get("referatNummerLista", []),
        "nyckelord": a.get("nyckelordLista", []),
        "rattsomrade": a.get("rattsomradeLista", []),
        "lagrum": a.get("lagrumLista", []),
        "forarbeten": a.get("forarbeteLista", []),
        "eu_avgoranden": a.get("europarattsligaAvgorandenLista", []),
        "hanvisade": a.get("hanvisadePubliceringarLista", []),
        "litteratur": a.get("litteraturLista", []),
        "ecli_nummer": a.get("ecliNummer"),
        "grupp_id": a.get("gruppKorrelationsnummer"),
        "har_html_fulltext": har_innehall,
        "bilagor": [
            {"filnamn": b.get("filnamn"), "fillagring_id": b.get("fillagringId")}
            for b in bilagor
        ],
    }

    if inkludera_innehall and har_innehall:
        result["innehall_html"] = a.get("innehall")
    elif not har_innehall and bilagor:
        result["info"] = (
            "Fulltext saknas i API:et för denna domstol. "
            "Använd hamta_pdf med fillagring_id från bilagor[] för att hämta PDF-text."
        )

    return result


# ---------------------------------------------------------------------------
# MCP-server och verktygsdeklarationer
# ---------------------------------------------------------------------------

server = Server("rattspraxis")


@server.list_tools()
async def lista_verktyg() -> list[types.Tool]:
    return [
        types.Tool(
            name="sok_rattpraxis",
            description=(
                "Söker i Domstolsverkets rättspraxis-databas (~17 500 avgöranden från svenska "
                "överrätter). Rättsfallsreferat finns från 1981; domar och beslut i fulltext "
                "finns från mars 2025. Returnerar sammanfattningar, lagrumshänvisningar och "
                "korsreferenser till förarbeten och EU-domstolsbeslut. "
                "Använd sfs_nummer för praxis kopplad till en specifik lag, "
                "domstolkoder=['HDO'] för enbart Högsta domstolens prejudikat."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "fritext": {
                        "type": "string",
                        "description": "Fritextsökning — ord AND-kombineras. Exempel: 'skadestånd entreprenad'",
                    },
                    "domstolkoder": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filtrera på domstol. Koder: HDO (HD), HFD, ADO (AD), "
                            "MMOD (MÖD), MIOD (Migrationsöverdomstolen), HSV (Svea hovrätt) m.fl. "
                            "HFD och REGR (Regeringsrätten, t.o.m. 2010) expanderas automatiskt "
                            "till båda — ange endera för att söka hela beståndet. "
                            "Detsamma gäller MMOD och MOD (Miljööverdomstolen, t.o.m. 2011)."
                        ),
                    },
                    "ar_vagledande": {
                        "type": "boolean",
                        "description": "true = bara prejudikat och vägledande avgöranden",
                    },
                    "rattsomrade": {
                        "type": "string",
                        "description": (
                            "Rättsområde. Alternativ: Miljömål, Skatt, Migrationsmål, "
                            "Brottmål inkl mål om utdömande av vite, Socialförsäkring m.fl."
                        ),
                    },
                    "sfs_nummer": {
                        "type": "string",
                        "description": "SFS-nummer för lag, t.ex. '1942:740' (rättegångsbalken)",
                    },
                    "datum_fran": {"type": "string", "description": "Från-datum ÅÅÅÅ-MM-DD"},
                    "datum_till": {"type": "string", "description": "Till-datum ÅÅÅÅ-MM-DD"},
                    "nyckelord": {
                        "type": "string",
                        "description": "Ämnesord från avgörandenas nyckelordslista",
                    },
                    "sid_index": {
                        "type": "integer",
                        "description": "Sidindex, 0-baserat (standard: 0)",
                    },
                    "antal_per_sida": {
                        "type": "integer",
                        "description": "Träffar per sida, 1–50 (standard: 10)",
                    },
                },
            },
        ),
        types.Tool(
            name="hamta_avgorande",
            description=(
                "Hämtar ett fullständigt avgörande med all metadata: lagrum, "
                "förarbeteshänvisningar, EU-rättshänvisningar, nyckelord och fulltext (HTML) "
                "om tillgänglig. HTML-fulltext finns för HFD m.fl. men saknas för HD (HDO) "
                "och MÖD (MMOD) — använd hamta_pdf för dessa."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "avgorande_id": {
                        "type": "string",
                        "description": "Avgörandets UUID (avgorande_id från sok_rattpraxis)",
                    },
                    "inkludera_html": {
                        "type": "boolean",
                        "description": "Inkludera HTML-fulltext i svaret om tillgänglig (standard: true)",
                    },
                    "hamta_kompanjon": {
                        "type": "boolean",
                        "description": (
                            "Hämta även syskonpublicering (DOM_ELLER_BESLUT↔REFERAT med NJA-nummer) "
                            "om tillgänglig (standard: false)"
                        ),
                    },
                },
                "required": ["avgorande_id"],
            },
        ),
        types.Tool(
            name="hamta_pdf",
            description=(
                "Hämtar och extraherar text ur PDF-bilagan till ett avgörande. "
                "Nödvändigt för HD (HDO) och MÖD (MMOD) som saknar HTML-fulltext i API:et. "
                "Extraherad text cachas lokalt — efterföljande anrop hämtar från cache. "
                "fillagring_id hämtas från bilagor[].fillagring_id i svaret från hamta_avgorande."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "fillagring_id": {
                        "type": "string",
                        "description": (
                            "Fillagrings-ID från bilagor[].fillagring_id, t.ex. '190/b6/74/uuid'. "
                            "Snedstrecken URL-kodas automatiskt."
                        ),
                    },
                    "avgorande_id": {
                        "type": "string",
                        "description": "UUID för avgörandet — används för att koppla PDF till avgörande i cachen (valfritt)",
                    },
                    "filnamn": {
                        "type": "string",
                        "description": "Filnamn på PDF:en, t.ex. 'B 712-25.pdf' (valfritt, för cachelogg)",
                    },
                    "max_tecken": {
                        "type": "integer",
                        "description": (
                            "Teckentak för den returnerade domtexten (standard 60 000, 0 = hela texten). "
                            "Sätt ett tak för långa domar så att svaret inte överskrider "
                            "storleksgränsen. Ett kapat svar avslutas med en rad som anger "
                            "hur mycket som visas och hur resten hämtas."
                        ),
                        "default": 60000,
                    },
                    "fran_tecken": {
                        "type": "integer",
                        "description": (
                            "Börja texten vid denna teckenposition — för att läsa vidare "
                            "där ett kapat svar slutade. Citera aldrig ur ett kapat utdrag."
                        ),
                        "default": 0,
                    },
                },
                "required": ["fillagring_id"],
            },
        ),
        types.Tool(
            name="sok_rattpraxis_for_lagrum",
            description=(
                "Söker rättspraxis kopplad till ett specifikt lagrum i en lag. "
                "Mer precist än sok_rattpraxis(sfs_nummer=...) eftersom det kan filtrera "
                "på specifik paragraf, t.ex. '36 §' eller '5 kap. 3 §'. "
                "Hämtar alla träffar för SFS-numret och filtrerar på paragraf."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sfs_nummer": {
                        "type": "string",
                        "description": "SFS-nummer, t.ex. '1962:700' (brottsbalken)",
                    },
                    "paragraf": {
                        "type": "string",
                        "description": (
                            "Paragraf att filtrera på, t.ex. '36 §', '5 kap. 3 §'. "
                            "Partiell matchning — '36 §' matchar även '36 a §'."
                        ),
                    },
                    "ar_vagledande": {
                        "type": "boolean",
                        "description": "true = bara prejudikat och vägledande avgöranden",
                    },
                    "datum_fran": {"type": "string", "description": "Från-datum ÅÅÅÅ-MM-DD"},
                    "datum_till": {"type": "string", "description": "Till-datum ÅÅÅÅ-MM-DD"},
                    "max_antal": {
                        "type": "integer",
                        "description": "Max antal träffar (standard: 20, max: 200)",
                    },
                },
                "required": ["sfs_nummer"],
            },
        ),
        types.Tool(
            name="hamta_avgorande_pa_beteckning",
            description=(
                "Hämtar ett avgörande via referensnummer eller kortnamn. Stöder:\n"
                "• NJA-nummer: 'NJA 2025:67' eller 'NJA 2025 s. 1024'\n"
                "• HFD-referat: 'HFD 2026 ref. 1'\n"
                "• MÖD: 'MÖD 2025:51', AD: 'AD 2024 nr 47'\n"
                "• HD:s kortnamn: '\"Ringa stöld-gränsen II\"'\n"
                "• Målnummer: 'B 712-25', 'Ö 6478-25'\n"
                "Returnerar avgörandet med kompanjonpublicering (DOM↔REFERAT) om tillgänglig."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "beteckning": {
                        "type": "string",
                        "description": (
                            "Referensnummer eller kortnamn. Exempel: 'NJA 2025:67', "
                            "'HFD 2026 ref. 1', '\"Ringa stöld-gränsen II\"', 'B 712-25'"
                        ),
                    },
                    "hamta_kompanjon": {
                        "type": "boolean",
                        "description": "Hämta syskonpublicering (DOM↔REFERAT) om tillgänglig (standard: true)",
                    },
                },
                "required": ["beteckning"],
            },
        ),
        types.Tool(
            name="sok_i_domtext",
            description=(
                "Söker fulltext inuti cachade domstolsavgöranden. "
                "Kräver att domar dessförinnan hämtats med hamta_pdf (texten cachas lokalt). "
                "Använd för att hitta specifika resonemang, lagcitat eller rättsliga principer "
                "i domtexterna — kompletterar metadata-sökning med sökning i domskälen. "
                "PostgreSQL: avancerad FTS med träffrelevans och utdrag. "
                "SQLite: enklare textsökning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sokterm": {
                        "type": "string",
                        "description": "Sökterm eller fras att leta efter i domtexterna",
                    },
                    "domstolkod": {
                        "type": "string",
                        "description": "Filtrera på domstol: HDO, HFD, MMOD m.fl. (valfritt)",
                    },
                    "max_antal": {
                        "type": "integer",
                        "description": "Max antal träffar (standard: 10, max: 50)",
                    },
                },
                "required": ["sokterm"],
            },
        ),
    ]


@server.call_tool()
async def anropa_verktyg(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    args = arguments or {}
    log.info("Verktygsanrop: %s %s", name, list(args.keys()))

    try:
        if name == "sok_rattpraxis":
            return await _sok_rattpraxis(**args)
        elif name == "hamta_avgorande":
            return await _hamta_avgorande(**args)
        elif name == "hamta_pdf":
            return await _hamta_pdf(**args)
        elif name == "sok_rattpraxis_for_lagrum":
            return await _sok_rattpraxis_for_lagrum(**args)
        elif name == "hamta_avgorande_pa_beteckning":
            return await _hamta_avgorande_pa_beteckning(**args)
        elif name == "sok_i_domtext":
            return await _sok_i_domtext(**args)
        else:
            return [types.TextContent(type="text", text=f"Okänt verktyg: {name}")]
    except Exception as e:
        log.error("Fel i %s: %s", name, e, exc_info=True)
        return [types.TextContent(type="text", text=f"Fel vid anrop till {name}: {e}")]


# ---------------------------------------------------------------------------
# Verktygsimplementationer
# ---------------------------------------------------------------------------

async def _sok_rattpraxis(
    fritext=None,
    domstolkoder=None,
    ar_vagledande=None,
    rattsomrade=None,
    sfs_nummer=None,
    datum_fran=None,
    datum_till=None,
    nyckelord=None,
    sid_index=0,
    antal_per_sida=10,
):
    antal_per_sida = min(int(antal_per_sida or 10), _MAX_PER_SIDA)
    sid_index = int(sid_index or 0)

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
    if ar_vagledande is not None:
        f["arVagledande"] = ar_vagledande
    if rattsomrade:
        f["rattsomradeLista"] = [rattsomrade]
    if sfs_nummer:
        f["sfsNummerLista"] = [sfs_nummer]
    _satt_datumintervall(f, datum_fran, datum_till)
    if nyckelord:
        f["sokordLista"] = [nyckelord]

    data = _sok_post(body)
    total = data.get("total", 0)
    treffar = data.get("publiceringLista", [])

    antal_sidor = (total + antal_per_sida - 1) // antal_per_sida if total > 0 else 0
    resultat = {
        "total": total,
        "sida": sid_index + 1,
        "antal_per_sida": antal_per_sida,
        "antal_sidor": antal_sidor,
        "avgoranden": [_formatera_avgorande(a) for a in treffar],
    }

    return [types.TextContent(type="text", text=json.dumps(resultat, ensure_ascii=False, indent=2))]


async def _hamta_avgorande(avgorande_id, inkludera_html=True, hamta_kompanjon=False):
    # Försök cache först
    a = _las_avgorande_cache(avgorande_id)
    kalla = "cache"

    if a is None:
        a = _hamta_publicering_api(avgorande_id)
        _skriv_avgorande_cache(a)
        kalla = "api"

    log.info("hamta_avgorande %s — källa: %s", avgorande_id, kalla)
    result = _formatera_avgorande(a, inkludera_innehall=bool(inkludera_html))

    if hamta_kompanjon:
        kompanjon = _hamta_grupp_kompanjon(a)
        result["kompanjon"] = _formatera_avgorande(kompanjon) if kompanjon else None

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


def _sakerstall_avgorande_cache(avgorande_id: str):
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
        a = _hamta_publicering_api(avgorande_id)
        _skriv_avgorande_cache(a)
        log.info("Metadata cachad för avgörande %s (via hamta_pdf)", avgorande_id)
    except Exception as e:
        log.warning("Kunde inte cacha metadata för avgörande %s: %s", avgorande_id, e)


async def _hamta_pdf(fillagring_id, avgorande_id=None, filnamn=None,
                     max_tecken=RP_MAX_TECKEN, fran_tecken=0):
    def _anvisning(fran_ny):
        return (
            f'Läs vidare: hamta_pdf(fillagring_id="{fillagring_id}", '
            f"fran_tecken={fran_ny})"
        )

    # Försök cache först
    cachad_text = _las_pdf_cache(fillagring_id)
    if cachad_text:
        log.info("hamta_pdf %s — returnerar från cache", fillagring_id)
        # Retroaktiv metadata-fyllning: säkerställ att avgorande_cache är
        # populerad även för PDF:er som cachades innan denna fix.
        _sakerstall_avgorande_cache(avgorande_id)
        return [types.TextContent(
            type="text",
            text=_skar_ut_text(cachad_text, max_tecken, fran_tecken,
                               _anvisning(fran_tecken + max_tecken)),
        )]

    # Importera pymupdf4llm (lazy — krävs bara när PDF-hämtning sker)
    try:
        import fitz
        import pymupdf4llm
    except ImportError:
        return [types.TextContent(
            type="text",
            text=(
                "pymupdf4llm är inte installerat. "
                "Kör: pip install pymupdf4llm\n"
                "Starta om MCP-servern efteråt."
            ),
        )]

    # Hämta PDF från Domstolsverkets API
    kodad_id = urllib.parse.quote(fillagring_id, safe="")
    url = f"{_API_BAS}/bilagor/{kodad_id}"
    log.info("Hämtar PDF: %s", url)

    try:
        # API:et levererar bilagor som application/pdf och svarar 406 på
        # andra Accept-värden.
        resp = requests.get(url, headers={**_HEADERS, "Accept": "application/pdf"}, timeout=30)
        resp.raise_for_status()
        pdf_bytes = resp.content
    except requests.RequestException as e:
        log.error("Fel vid PDF-hämtning från %s: %s", url, e)
        return [types.TextContent(type="text", text=f"Fel vid PDF-hämtning: {e}")]

    # Extrahera text — FD 1 skyddas mot C-bindningarnas utskrifter
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        with _tysta_fd1():
            markdown_text = pymupdf4llm.to_markdown(doc)
        doc.close()
        log.info("PDF extraherad: %d tecken, %d bytes", len(markdown_text), len(pdf_bytes))
    except Exception as e:
        log.error("Fel vid PDF-extraktion: %s", e)
        return [types.TextContent(type="text", text=f"Fel vid PDF-extraktion: {e}")]

    # Cacha för framtida anrop
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
    return [types.TextContent(
        type="text",
        text=_skar_ut_text(markdown_text, max_tecken, fran_tecken,
                           _anvisning(fran_tecken + max_tecken)),
    )]


async def _sok_rattpraxis_for_lagrum(
    sfs_nummer,
    paragraf=None,
    ar_vagledande=None,
    datum_fran=None,
    datum_till=None,
    max_antal=20,
):
    max_antal = min(int(max_antal or 20), 200)
    para_filter = (paragraf or "").lower().strip()

    body = {
        "sokfras": {"andLista": [], "exaktFras": None},
        "filter": {"sfsNummerLista": [sfs_nummer]},
        "sortorder": "desc",
        "sidIndex": 0,
        "antalPerSida": _MAX_PER_SIDA,
    }

    f = body["filter"]
    if ar_vagledande is not None:
        f["arVagledande"] = ar_vagledande
    _satt_datumintervall(f, datum_fran, datum_till)

    # Notera: sok_rattpraxis_for_lagrum filtrerar inte på domstol via API —
    # domstolsfiltrering kan läggas till som parameter i framtida version om behov uppstår

    alla = []
    sid = 0

    while len(alla) < max_antal:
        body["sidIndex"] = sid
        data = _sok_post(body)
        treffar = data.get("publiceringLista", [])
        total = data.get("total", 0)

        if not treffar:
            break

        if para_filter:
            for a in treffar:
                for lagrum in a.get("lagrumLista", []):
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

    resultat = {
        "sfs_nummer": sfs_nummer,
        "paragraf_filter": paragraf,
        "antal_treffar": len(alla),
        "avgoranden": [_formatera_avgorande(a) for a in alla],
    }

    return [types.TextContent(type="text", text=json.dumps(resultat, ensure_ascii=False, indent=2))]


async def _hamta_avgorande_pa_beteckning(beteckning, hamta_kompanjon=True):
    beteckning = (beteckning or "").strip()

    # Steg 1: exaktFras-sökning (hanterar NJA-nummer, HFD-ref, kortnamn)
    body = {
        "sokfras": {"andLista": [], "exaktFras": beteckning},
        "filter": {},
        "sortorder": "desc",
        "sidIndex": 0,
        "antalPerSida": 10,
    }
    data = _sok_post(body)
    treffar = data.get("publiceringLista", [])

    # Steg 2: AND-sökning som reservväg
    if not treffar:
        ord_lista = [w for w in beteckning.replace('"', "").split() if len(w) > 2]
        if ord_lista:
            body["sokfras"] = {"andLista": ord_lista, "exaktFras": None}
            data = _sok_post(body)
            treffar = data.get("publiceringLista", [])

    if not treffar:
        return [types.TextContent(
            type="text",
            text=json.dumps({
                "beteckning": beteckning,
                "hittades": False,
                "meddelande": f"Inga avgöranden hittades för '{beteckning}'.",
            }, ensure_ascii=False),
        )]

    huvud = treffar[0]

    # Hämta fullständigt avgörande (via cache eller API)
    a = _las_avgorande_cache(huvud["id"])
    if a is None:
        try:
            a = _hamta_publicering_api(huvud["id"])
            _skriv_avgorande_cache(a)
        except Exception:
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

        # Annars sök via benamning
        if kompanjon is None:
            kompanjon = _hamta_grupp_kompanjon(a)

        result["kompanjon"] = _formatera_avgorande(kompanjon) if kompanjon else None

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def _sok_i_domtext(sokterm, domstolkod=None, max_antal=10):
    max_antal = min(int(max_antal or 10), 50)
    # Expandera domstolkod till historiska alias (HFD↔REGR, MMOD↔MOD)
    domstolkoder_expanded = _expandera_domstolkoder([domstolkod]) if domstolkod else None

    if not _DATABASE_URL:
        return [types.TextContent(
            type="text",
            text="sok_i_domtext kräver en konfigurerad DATABASE_URL i .env-filen.",
        )]

    try:
        conn = _hamta_db()
        cur = conn.cursor()

        if _ar_postgres():
            # PostgreSQL FTS med ts_headline för kontextutdrag
            # Joinar mot avgorande_cache för metadata och domstolsfiltrering
            domstol_filter = ""
            params: list = [sokterm, sokterm, sokterm]

            if domstolkoder_expanded:
                placeholders = ", ".join(["%s"] * len(domstolkoder_expanded))
                domstol_filter = f"AND ac.domstolkod IN ({placeholders})"
                params.extend(domstolkoder_expanded)

            params.append(max_antal)

            fraga = f"""
                SELECT
                    pc.fillagring_id,
                    pc.avgorande_id,
                    ac.domstolkod,
                    ac.avgorandedatum,
                    ac.benamning,
                    ac.data->>'sammanfattning'        AS sammanfattning,
                    ts_rank(pc.text_tsv,
                        plainto_tsquery('swedish', %s)) AS rank,
                    ts_headline(
                        'swedish', pc.text_md,
                        plainto_tsquery('swedish', %s),
                        'MaxFragments=3, MaxWords=40, MinWords=15,
                         StartSel=>>>, StopSel=<<<'
                    )                                 AS utdrag
                FROM rattspraxis.pdf_cache pc
                LEFT JOIN rattspraxis.avgorande_cache ac
                    ON ac.id = pc.avgorande_id
                WHERE pc.text_tsv @@ plainto_tsquery('swedish', %s)
                {domstol_filter}
                ORDER BY rank DESC
                LIMIT %s
            """
            cur.execute(fraga, params)
            rader = cur.fetchall()

            treffar = []
            for rad in rader:
                treffar.append({
                    "fillagring_id": rad[0],
                    "avgorande_id": rad[1],
                    "domstolkod": rad[2],
                    "avgorandedatum": str(rad[3]) if rad[3] else None,
                    "benamning": rad[4],
                    "sammanfattning": rad[5],
                    "relevans": float(rad[6]) if rad[6] else 0.0,
                    "utdrag": rad[7],
                })

        else:
            # SQLite — enkel LIKE-sökning
            if domstolkoder_expanded:
                placeholders = ", ".join(["?"] * len(domstolkoder_expanded))
                domstol_filter = f"AND domstolkod IN ({placeholders})"
            else:
                domstol_filter = ""
            params_sqlite: list = [f"%{sokterm}%"]
            if domstolkoder_expanded:
                params_sqlite.extend(domstolkoder_expanded)
            params_sqlite.append(max_antal)

            cur.execute(f"""
                SELECT
                    pc.fillagring_id,
                    pc.avgorande_id,
                    pc.filnamn,
                    substr(pc.text_md, instr(lower(pc.text_md), lower(?)) - 100, 300) AS utdrag
                FROM pdf_cache pc
                LEFT JOIN avgorande_cache ac ON ac.id = pc.avgorande_id
                WHERE lower(pc.text_md) LIKE lower(?)
                {domstol_filter}
                LIMIT ?
            """, [sokterm] + params_sqlite)
            rader = cur.fetchall()

            treffar = []
            for rad in rader:
                treffar.append({
                    "fillagring_id": rad[0],
                    "avgorande_id": rad[1],
                    "filnamn": rad[2],
                    "utdrag": rad[3],
                })

        conn.close()

        resultat = {
            "sokterm": sokterm,
            "antal_treffar": len(treffar),
            "info": (
                "Söker bara i domar som tidigare hämtats med hamta_pdf."
                if not treffar else None
            ),
            "treffar": treffar,
        }
        if treffar:
            del resultat["info"]

        return [types.TextContent(type="text", text=json.dumps(resultat, ensure_ascii=False, indent=2))]

    except Exception as e:
        log.error("Fel i sok_i_domtext: %s", e, exc_info=True)
        return [types.TextContent(type="text", text=f"Fel vid textsökning: {e}")]


# ---------------------------------------------------------------------------
# Transporter
# ---------------------------------------------------------------------------

async def _kora_stdio():
    async with stdio_server() as (las, skriv):
        await server.run(las, skriv, server.create_initialization_options())


def _starta_http():
    """
    Startar HTTP-transport via StreamableHTTP-protokollet med valfri
    Bearer-token-autentisering.

    Servern lyssnar på /mcp och hanterar sessioner via
    StreamableHTTPSessionManager. Lifespan-kontexthanteraren säkerställer
    att session manager startas och stängs ned korrekt med Starlette.
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    import uvicorn

    api_nyckel = os.getenv("MCP_API_KEY", "")
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8005"))

    session_manager = StreamableHTTPSessionManager(server)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            yield

    async def hantera_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    middleware_lista = []
    if api_nyckel:
        log.info("API-nyckelautentisering aktiverad")

        class BearerKontroll(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                token = (
                    request.headers.get("Authorization", "")
                    .removeprefix("Bearer ")
                    .strip()
                )
                # secrets.compare_digest ger konstant-tidsjämförelse (skyddar mot timing-attack).
                if not secrets.compare_digest(token, api_nyckel):
                    return PlainTextResponse(
                        "Obehörig: ogiltig eller saknad API-nyckel.", status_code=401
                    )
                return await call_next(request)

        middleware_lista = [Middleware(BearerKontroll)]
    else:
        log.warning(
            "MCP_API_KEY är inte satt — servern körs utan autentisering. "
            "Bind enbart till loopback (MCP_HOST=127.0.0.1) eller "
            "skydda via reverse proxy."
        )

    app = Starlette(
        lifespan=lifespan,
        routes=[Mount("/mcp", app=hantera_mcp)],
        middleware=middleware_lista,
    )

    log.info("Startar HTTP-transport på %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    try:
        _sakerstall_schema()
    except Exception as e:
        log.warning("Schema-init misslyckades (%s) — servern startar ändå.", e)

    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        log.info("Startar rattspraxis MCP-server (stdio)")
        asyncio.run(_kora_stdio())
    elif transport == "http":
        _starta_http()
    else:
        log.error("Okänt MCP_TRANSPORT: %s", transport)
        sys.exit(1)


if __name__ == "__main__":
    main()
