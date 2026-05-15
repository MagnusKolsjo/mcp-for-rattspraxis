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
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

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
_MAX_PER_SIDA = 50

_DATABASE_URL = os.getenv("DATABASE_URL", "")
_PDF_CACHE_TTL_DAGAR = int(os.getenv("PDF_CACHE_TTL_DAGAR", "90"))
_METADATA_CACHE_TTL_DAGAR = int(os.getenv("METADATA_CACHE_TTL_DAGAR", "30"))

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
# Databaslager
# ---------------------------------------------------------------------------

def _ar_postgres() -> bool:
    """Returnerar True om DATABASE_URL pekar på PostgreSQL."""
    return _DATABASE_URL.startswith("postgresql")


def _hamta_db():
    """Öppnar och returnerar en databasanslutning (PostgreSQL eller SQLite)."""
    if _ar_postgres():
        import psycopg2
        return psycopg2.connect(_DATABASE_URL)
    else:
        import sqlite3
        db_fil = _DATABASE_URL.replace("sqlite:///", "") or "rattspraxis_cache.db"
        if not os.path.isabs(db_fil):
            db_fil = str(_SCRIPT_DIR / db_fil)
        return sqlite3.connect(db_fil)


def _sakerstall_schema():
    """
    Skapar tabeller och index om de inte finns.
    Körs vid serverstart.
    """
    try:
        conn = _hamta_db()
        cur = conn.cursor()

        if _ar_postgres():
            cur.execute("CREATE SCHEMA IF NOT EXISTS rattspraxis")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rattspraxis.avgorande_cache (
                    id             TEXT PRIMARY KEY,
                    domstolkod     TEXT,
                    avgorandedatum DATE,
                    ar_vagledande  BOOLEAN,
                    benamning      TEXT,
                    data           JSONB NOT NULL,
                    hamtat         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ttl_expires    TIMESTAMPTZ NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS avgorande_cache_domstol_idx
                    ON rattspraxis.avgorande_cache (domstolkod)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS avgorande_cache_datum_idx
                    ON rattspraxis.avgorande_cache (avgorandedatum)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rattspraxis.pdf_cache (
                    fillagring_id     TEXT PRIMARY KEY,
                    avgorande_id      TEXT,
                    text_md           TEXT NOT NULL,
                    text_tsv          TSVECTOR
                                        GENERATED ALWAYS AS
                                        (to_tsvector('swedish', text_md)) STORED,
                    hamtat            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ttl_expires       TIMESTAMPTZ NOT NULL,
                    filstorlek_bytes  INTEGER,
                    filnamn           TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS pdf_cache_fts_idx
                    ON rattspraxis.pdf_cache USING GIN (text_tsv)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS pdf_cache_avgorande_idx
                    ON rattspraxis.pdf_cache (avgorande_id)
            """)
        else:
            # SQLite — enklare schema utan GENERATED ALWAYS och JSON
            cur.execute("""
                CREATE TABLE IF NOT EXISTS avgorande_cache (
                    id             TEXT PRIMARY KEY,
                    domstolkod     TEXT,
                    avgorandedatum TEXT,
                    ar_vagledande  INTEGER,
                    benamning      TEXT,
                    data           TEXT NOT NULL,
                    hamtat         TEXT NOT NULL,
                    ttl_expires    TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pdf_cache (
                    fillagring_id     TEXT PRIMARY KEY,
                    avgorande_id      TEXT,
                    text_md           TEXT NOT NULL,
                    hamtat            TEXT NOT NULL,
                    ttl_expires       TEXT NOT NULL,
                    filstorlek_bytes  INTEGER,
                    filnamn           TEXT
                )
            """)

        conn.commit()
        conn.close()
        log.info("Databasschema verifierat")
    except Exception as e:
        log.warning("Kunde inte säkerställa databasschema: %s", e)


def _nu_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ttl_expires(dagar: int) -> datetime:
    return _nu_utc() + timedelta(days=dagar)


def _ar_giltig(ttl_expires_str: str) -> bool:
    """Returnerar True om TTL-tidsstämpeln inte har passerat."""
    try:
        if isinstance(ttl_expires_str, datetime):
            exp = ttl_expires_str
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp > _nu_utc()
        exp = datetime.fromisoformat(str(ttl_expires_str).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > _nu_utc()
    except Exception:
        return False


# --- Avgorande-cache ---

def _las_avgorande_cache(avgorande_id: str) -> dict | None:
    """Hämtar ett cachat avgörande om det finns och inte har gått ut."""
    try:
        conn = _hamta_db()
        cur = conn.cursor()
        if _ar_postgres():
            cur.execute(
                "SELECT data, ttl_expires FROM rattspraxis.avgorande_cache WHERE id = %s",
                (avgorande_id,)
            )
        else:
            cur.execute(
                "SELECT data, ttl_expires FROM avgorande_cache WHERE id = ?",
                (avgorande_id,)
            )
        rad = cur.fetchone()
        conn.close()
        if rad and _ar_giltig(rad[1]):
            data = rad[0]
            return json.loads(data) if isinstance(data, str) else data
    except Exception as e:
        log.warning("Fel vid läsning av avgorande_cache: %s", e)
    return None


def _skriv_avgorande_cache(a: dict):
    """Lagrar ett avgörande i cachen."""
    try:
        conn = _hamta_db()
        cur = conn.cursor()
        ttl = _ttl_expires(_METADATA_CACHE_TTL_DAGAR)
        avgorande_id = a.get("id", "")
        domstolkod = (a.get("domstol") or {}).get("domstolKod")
        avgorandedatum = a.get("avgorandedatum")
        ar_vagledande = a.get("arVagledande")
        benamning = a.get("benamning")
        nu = _nu_utc().isoformat()

        if _ar_postgres():
            cur.execute("""
                INSERT INTO rattspraxis.avgorande_cache
                    (id, domstolkod, avgorandedatum, ar_vagledande, benamning, data, hamtat, ttl_expires)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    hamtat = EXCLUDED.hamtat,
                    ttl_expires = EXCLUDED.ttl_expires
            """, (avgorande_id, domstolkod, avgorandedatum, ar_vagledande, benamning,
                  json.dumps(a, ensure_ascii=False), nu, ttl.isoformat()))
        else:
            cur.execute("""
                INSERT OR REPLACE INTO avgorande_cache
                    (id, domstolkod, avgorandedatum, ar_vagledande, benamning, data, hamtat, ttl_expires)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (avgorande_id, domstolkod, avgorandedatum,
                  1 if ar_vagledande else 0, benamning,
                  json.dumps(a, ensure_ascii=False), nu, ttl.isoformat()))

        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Fel vid skrivning till avgorande_cache: %s", e)


# --- PDF-cache ---

def _las_pdf_cache(fillagring_id: str) -> str | None:
    """Hämtar cachad PDF-text om den finns och inte gått ut."""
    try:
        conn = _hamta_db()
        cur = conn.cursor()
        tabell = "rattspraxis.pdf_cache" if _ar_postgres() else "pdf_cache"
        plats = "%s" if _ar_postgres() else "?"
        cur.execute(
            f"SELECT text_md, ttl_expires FROM {tabell} WHERE fillagring_id = {plats}",
            (fillagring_id,)
        )
        rad = cur.fetchone()
        conn.close()
        if rad and _ar_giltig(rad[1]):
            return rad[0]
    except Exception as e:
        log.warning("Fel vid läsning av pdf_cache: %s", e)
    return None


def _skriv_pdf_cache(
    fillagring_id: str,
    text_md: str,
    avgorande_id: str = None,
    filstorlek: int = None,
    filnamn: str = None,
):
    """Lagrar extraherad PDF-text i cachen."""
    try:
        conn = _hamta_db()
        cur = conn.cursor()
        ttl = _ttl_expires(_PDF_CACHE_TTL_DAGAR)
        nu = _nu_utc().isoformat()

        if _ar_postgres():
            cur.execute("""
                INSERT INTO rattspraxis.pdf_cache
                    (fillagring_id, avgorande_id, text_md, hamtat, ttl_expires,
                     filstorlek_bytes, filnamn)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fillagring_id) DO UPDATE SET
                    text_md = EXCLUDED.text_md,
                    hamtat = EXCLUDED.hamtat,
                    ttl_expires = EXCLUDED.ttl_expires
            """, (fillagring_id, avgorande_id, text_md, nu, ttl.isoformat(),
                  filstorlek, filnamn))
        else:
            cur.execute("""
                INSERT OR REPLACE INTO pdf_cache
                    (fillagring_id, avgorande_id, text_md, hamtat, ttl_expires,
                     filstorlek_bytes, filnamn)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (fillagring_id, avgorande_id, text_md, nu, ttl.isoformat(),
                  filstorlek, filnamn))

        conn.commit()
        conn.close()
        log.info("PDF-text cachad: %s (%d tecken)", fillagring_id, len(text_md))
    except Exception as e:
        log.warning("Fel vid skrivning till pdf_cache: %s", e)


# ---------------------------------------------------------------------------
# HTTP-hjälpfunktioner mot Domstolsverkets API
# ---------------------------------------------------------------------------

def _sok_post(body: dict) -> dict:
    """Anropar POST /api/v1/sok och returnerar svaret som dict."""
    url = f"{_API_BAS}/sok"
    r = requests.post(url, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _hamta_publicering_api(avgorande_id: str) -> dict:
    """GET /api/v1/publiceringar/{id} — returnerar fullständigt avgörande."""
    url = f"{_API_BAS}/publiceringar/{avgorande_id}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Intern hjälpfunktion: grupp-kompanjon (DOM_ELLER_BESLUT ↔ REFERAT)
# ---------------------------------------------------------------------------

def _hamta_grupp_kompanjon(avgorande: dict) -> dict | None:
    """
    Slår upp syskonpublikation via benamning-exaktsökning.

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
                    "id": {
                        "type": "string",
                        "description": "Avgörandets UUID (id-fältet från sok_rattpraxis)",
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
                "required": ["id"],
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
# Verktygsiimplementationer
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
    if datum_fran:
        f["fromDatum"] = datum_fran
    if datum_till:
        f["toDatum"] = datum_till
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


async def _hamta_avgorande(id, inkludera_html=True, hamta_kompanjon=False):
    # Försök cache först
    a = _las_avgorande_cache(id)
    kalla = "cache"

    if a is None:
        a = _hamta_publicering_api(id)
        _skriv_avgorande_cache(a)
        kalla = "api"

    log.info("hamta_avgorande %s — källa: %s", id, kalla)
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


async def _hamta_pdf(fillagring_id, avgorande_id=None, filnamn=None):
    # Försök cache först
    cachad_text = _las_pdf_cache(fillagring_id)
    if cachad_text:
        log.info("hamta_pdf %s — returnerar från cache", fillagring_id)
        # Retroaktiv metadata-fyllning: säkerställ att avgorande_cache är
        # populerad även för PDF:er som cachades innan denna fix.
        _sakerstall_avgorande_cache(avgorande_id)
        return [types.TextContent(type="text", text=cachad_text)]

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
        resp = requests.get(url, headers={"Accept": "application/octet-stream"}, timeout=30)
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

    return [types.TextContent(type="text", text=markdown_text)]


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
    if datum_fran:
        f["fromDatum"] = datum_fran
    if datum_till:
        f["toDatum"] = datum_till

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
                    if para_filter in (lagrum.get("referens") or "").lower():
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

    # Steg 2: AND-sökning som fallback
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
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response
    from mcp.server.sse import SseServerTransport
    import uvicorn

    api_nyckel = os.getenv("MCP_API_KEY", "")
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8005"))

    class BearerKontroll(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if api_nyckel:
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] != api_nyckel:
                    return Response("Otillåten åtkomst", status_code=401)
            return await call_next(request)

    sse = SseServerTransport("/messages/")

    async def hantera_sse(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    app = Starlette(routes=[])
    app.add_middleware(BearerKontroll)
    app.add_route("/sse", hantera_sse)
    app.mount("/messages/", sse.handle_post_message)

    log.info("Startar HTTP-server på %s:%s", host, port)
    uvicorn.run(app, host=host, port=port)


def main():
    _sakerstall_schema()
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
