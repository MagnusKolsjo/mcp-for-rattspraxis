#!/usr/bin/env python3
"""
Databaslager för rättspraxis-MCP-servern.

Hanterar anslutning till PostgreSQL eller SQLite, schemaskapande och
cache-operationer för avgöranden och PDF-texter.
Konfiguration läses från .env via DATABASE_URL.
"""

import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
PDF_CACHE_TTL_DAGAR = int(os.getenv("PDF_CACHE_TTL_DAGAR", "90"))
METADATA_CACHE_TTL_DAGAR = int(os.getenv("METADATA_CACHE_TTL_DAGAR", "30"))


# ---------------------------------------------------------------------------
# Avgörandetyp
# ---------------------------------------------------------------------------
#
# API:et har inget eget fält för om ett avgörande är vägledande. Det framgår
# av avgörandetypen (typ): prejudikat och vägledande avgöranden mot avgöranden
# som uttryckligen inte är vägledande och beslut om prövningstillstånd.

VAGLEDANDE_TYPER = ["PREJUDIKAT", "VAGLEDANDE_MEN_EJ_PREJUDICERANDE"]
EJ_VAGLEDANDE_TYPER = ["EJ_VAGLEDANDE", "PROVNINGSTILLSTAND"]


def ar_vagledande(a: dict) -> bool | None:
    """True/False utifrån avgörandetypen, None om typen saknas eller är okänd."""
    typ = a.get("typ")
    if typ in VAGLEDANDE_TYPER:
        return True
    if typ in EJ_VAGLEDANDE_TYPER:
        return False
    return None


# ---------------------------------------------------------------------------
# Backend-hjälpfunktioner
# ---------------------------------------------------------------------------

def _ar_postgres() -> bool:
    """Returnerar True om DATABASE_URL pekar på PostgreSQL."""
    return DATABASE_URL.startswith(("postgresql", "postgres://"))


def _hamta_db():
    """Öppnar och returnerar en databasanslutning (PostgreSQL eller SQLite)."""
    if _ar_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    import sqlite3
    # sqlite:///relativ.db och sqlite:////absolut/sokvag.db. Det som följer
    # efter de tre snedstrecken är sökvägen; ett fjärde gör den absolut.
    if DATABASE_URL.startswith("sqlite:///"):
        db_fil = DATABASE_URL[len("sqlite:///"):]
    else:
        db_fil = DATABASE_URL
    db_fil = db_fil or "rattspraxis_cache.db"
    if not os.path.isabs(db_fil):
        db_fil = str(_SCRIPT_DIR / db_fil)
    return sqlite3.connect(db_fil)


def _ph() -> str:
    """Returnerar platshållarkaraktären för parameterbindning (%s eller ?)."""
    return "%s" if _ar_postgres() else "?"


def _prefix() -> str:
    """Returnerar tabellprefix: 'rattspraxis.' för PostgreSQL, tomt för SQLite."""
    return "rattspraxis." if _ar_postgres() else ""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _sakerstall_schema():
    """
    Skapar tabeller och index om de inte finns.
    Körs vid serverstart — idempotent.

    Baseline: v1.0.0 (2026-05-11). Bas-schemat ändras aldrig; nya kolumner,
    index och tabeller läggs i migrationsblocket.
    Migrationer:
      1. Sökbar text och fulltextindex i avgorande_cache, tabellen
         synk_status (efter v1.2.0)
    """
    try:
        conn = _hamta_db()
        cur = conn.cursor()

        if _ar_postgres():
            # ---------------------------------------------------------------
            # Baseline v1.0.0
            # ---------------------------------------------------------------
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
            # ---------------------------------------------------------------
            # Baseline v1.0.0 — SQLite-variant (enklare schema utan
            # GENERATED ALWAYS och JSONB; FTS görs via LIKE)
            # ---------------------------------------------------------------
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

        # ---------------------------------------------------------------
        # Migrationer — nya ALTER TABLE IF NOT EXISTS-block läggs här,
        # i kronologisk ordning.
        # ---------------------------------------------------------------

        _migration_1_sokbar_text(cur)

        conn.commit()

        # Datamigrationer i egna transaktioner, så att ett fel där inte
        # rullar tillbaka schemaändringarna ovan.
        _fyll_sokbar_text(conn)

        conn.close()
        log.info("Databasschema verifierat")
    except Exception as e:
        log.warning("Kunde inte säkerställa databasschema: %s", e)


def _kolumn_finns(cur, tabell: str, kolumn: str) -> bool:
    """SQLite saknar ADD COLUMN IF NOT EXISTS; kontrollera i förväg."""
    cur.execute(f"PRAGMA table_info({tabell})")
    return any(rad[1] == kolumn for rad in cur.fetchall())


def _migration_1_sokbar_text(cur) -> None:
    """
    Migration 1: sokbar_text i avgorande_cache och tabellen synk_status.

    sokbar_text är avgörandets benämning, referatnummer, sammanfattning,
    nyckelord och HTML-fulltext som ren text. Den gör att sok_i_domtext kan
    söka i alla lokalt lagrade avgöranden, inte bara i PDF-texterna.
    synk_status håller läget för synkskriptet mellan körningarna.
    """
    if _ar_postgres():
        cur.execute("""
            ALTER TABLE rattspraxis.avgorande_cache
                ADD COLUMN IF NOT EXISTS sokbar_text TEXT
        """)
        cur.execute("""
            ALTER TABLE rattspraxis.avgorande_cache
                ADD COLUMN IF NOT EXISTS sokbar_tsv TSVECTOR
                    GENERATED ALWAYS AS
                    (to_tsvector('swedish', coalesce(sokbar_text, ''))) STORED
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS avgorande_cache_fts_idx
                ON rattspraxis.avgorande_cache USING GIN (sokbar_tsv)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rattspraxis.synk_status (
                jobb                     TEXT PRIMARY KEY,
                status                   TEXT NOT NULL,
                startad                  TIMESTAMPTZ,
                avslutad                 TIMESTAMPTZ,
                fran_datum               TEXT,
                antal_sidor              INTEGER,
                antal_publiceringar      INTEGER,
                senaste_publiceringstid  TEXT,
                fullsynk_klar            TIMESTAMPTZ,
                meddelande               TEXT
            )
        """)
    else:
        if not _kolumn_finns(cur, "avgorande_cache", "sokbar_text"):
            cur.execute("ALTER TABLE avgorande_cache ADD COLUMN sokbar_text TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS synk_status (
                jobb                     TEXT PRIMARY KEY,
                status                   TEXT NOT NULL,
                startad                  TEXT,
                avslutad                 TEXT,
                fran_datum               TEXT,
                antal_sidor              INTEGER,
                antal_publiceringar      INTEGER,
                senaste_publiceringstid  TEXT,
                fullsynk_klar            TEXT,
                meddelande               TEXT
            )
        """)


def _fyll_sokbar_text(conn) -> None:
    """
    Fyller sokbar_text för rader som cachades innan kolumnen fanns.
    Idempotent: bara rader där sokbar_text saknas berörs.
    """
    cur = conn.cursor()
    cur.execute(f"SELECT id FROM {_prefix()}avgorande_cache WHERE sokbar_text IS NULL")
    idn = [rad[0] for rad in cur.fetchall()]
    if not idn:
        return
    for i in range(0, len(idn), 200):
        del_idn = idn[i:i + 200]
        platser = ", ".join([_ph()] * len(del_idn))
        cur.execute(
            f"SELECT id, data FROM {_prefix()}avgorande_cache WHERE id IN ({platser})",
            del_idn,
        )
        for avgorande_id, data in cur.fetchall():
            a = json.loads(data) if isinstance(data, str) else data
            cur.execute(
                f"UPDATE {_prefix()}avgorande_cache SET sokbar_text = {_ph()} "
                f"WHERE id = {_ph()}",
                (sokbar_text(a), avgorande_id),
            )
        conn.commit()
    log.info("sokbar_text ifylld för %d cachade avgöranden", len(idn))


# ---------------------------------------------------------------------------
# Sökbar text
# ---------------------------------------------------------------------------

_HTML_BLOCK = re.compile(
    r"</?(?:p|div|br|li|ul|ol|tr|td|th|table|h[1-6]|section|article)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_TAGG = re.compile(r"<[^>]+>")


def html_till_text(html_text: str | None) -> str:
    """Gör avgörandets HTML-fulltext till ren text med bevarade stycken."""
    if not html_text:
        return ""
    text = _HTML_BLOCK.sub("\n", html_text)
    text = _HTML_TAGG.sub("", text)
    text = html.unescape(text).replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return text.strip()


def sokbar_text(a: dict) -> str:
    """
    Den text sok_i_domtext söker i för ett avgörande: benämning,
    referatnummer, sammanfattning, nyckelord och fulltext.

    Domar och beslut som bara finns som PDF (HD och MÖD) saknar fulltext här;
    de blir sökbara på sammanfattningen, och i sin helhet när PDF:en hämtats
    med hamta_pdf.
    """
    delar = [
        a.get("benamning") or "",
        " ".join(a.get("referatNummerLista") or []),
        a.get("sammanfattning") or "",
        ", ".join(a.get("nyckelordLista") or []),
        html_till_text(a.get("innehall")),
    ]
    return "\n\n".join(d.strip() for d in delar if d and d.strip())


# ---------------------------------------------------------------------------
# Tidhjälpfunktioner
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Avgörande-cache
# ---------------------------------------------------------------------------

def _las_avgorande_cache(avgorande_id: str) -> dict | None:
    """Hämtar ett cachat avgörande om det finns och inte har gått ut."""
    try:
        conn = _hamta_db()
        cur = conn.cursor()
        cur.execute(
            f"SELECT data, ttl_expires FROM {_prefix()}avgorande_cache WHERE id = {_ph()}",
            (avgorande_id,),
        )
        rad = cur.fetchone()
        conn.close()
        if rad and _ar_giltig(rad[1]):
            data = rad[0]
            return json.loads(data) if isinstance(data, str) else data
    except Exception as e:
        log.warning("Fel vid läsning av avgorande_cache: %s", e)
    return None


def _upsert_avgorande(cur, a: dict, nu: str, ttl: str) -> None:
    """Skriver ett avgörande med sökbar text. Befintlig rad skrivs över helt."""
    rad = (
        a.get("id", ""),
        (a.get("domstol") or {}).get("domstolKod"),
        a.get("avgorandedatum"),
        ar_vagledande(a),
        a.get("benamning"),
        json.dumps(a, ensure_ascii=False),
        nu,
        ttl,
        sokbar_text(a),
    )
    if _ar_postgres():
        cur.execute("""
            INSERT INTO rattspraxis.avgorande_cache
                (id, domstolkod, avgorandedatum, ar_vagledande, benamning, data,
                 hamtat, ttl_expires, sokbar_text)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                domstolkod = EXCLUDED.domstolkod,
                avgorandedatum = EXCLUDED.avgorandedatum,
                ar_vagledande = EXCLUDED.ar_vagledande,
                benamning = EXCLUDED.benamning,
                data = EXCLUDED.data,
                hamtat = EXCLUDED.hamtat,
                ttl_expires = EXCLUDED.ttl_expires,
                sokbar_text = EXCLUDED.sokbar_text
        """, rad)
    else:
        vagledande = rad[3]
        cur.execute("""
            INSERT OR REPLACE INTO avgorande_cache
                (id, domstolkod, avgorandedatum, ar_vagledande, benamning, data,
                 hamtat, ttl_expires, sokbar_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rad[:3] + (None if vagledande is None else int(vagledande),) + rad[4:])


def _skriv_avgorande_cache(a: dict):
    """Lagrar ett avgörande i cachen. Fel loggas; cachen är inte kritisk för svaret."""
    try:
        conn = _hamta_db()
        try:
            _upsert_avgorande(
                conn.cursor(), a, _nu_utc().isoformat(),
                _ttl_expires(METADATA_CACHE_TTL_DAGAR).isoformat(),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("Fel vid skrivning till avgorande_cache: %s", e)


def skriv_avgoranden(avgoranden: list[dict]) -> int:
    """
    Skriver en sida publiceringar från synken i en transaktion.

    Till skillnad från _skriv_avgorande_cache kastas fel vidare: synken
    ska avbrytas och kunna köras om, inte tyst fortsätta utan data.
    """
    conn = _hamta_db()
    try:
        cur = conn.cursor()
        nu = _nu_utc().isoformat()
        ttl = _ttl_expires(METADATA_CACHE_TTL_DAGAR).isoformat()
        for a in avgoranden:
            if a.get("id"):
                _upsert_avgorande(cur, a, nu, ttl)
        conn.commit()
    finally:
        conn.close()
    return len(avgoranden)


# ---------------------------------------------------------------------------
# Synkstatus
# ---------------------------------------------------------------------------

_SYNK_FALT = [
    "status", "startad", "avslutad", "fran_datum", "antal_sidor",
    "antal_publiceringar", "senaste_publiceringstid", "fullsynk_klar", "meddelande",
]


def las_synk_status(jobb: str) -> dict | None:
    """Läser synkläget för ett jobb, eller None om jobbet aldrig körts."""
    conn = _hamta_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_SYNK_FALT)} FROM {_prefix()}synk_status "
            f"WHERE jobb = {_ph()}",
            (jobb,),
        )
        rad = cur.fetchone()
    finally:
        conn.close()
    if rad is None:
        return None
    return {
        falt: (varde.isoformat() if isinstance(varde, datetime) else varde)
        for falt, varde in zip(_SYNK_FALT, rad)
    }


def spara_synk_status(jobb: str, **falt) -> None:
    """Uppdaterar de angivna fälten i synkläget; övriga behåller sitt värde."""
    okanda = set(falt) - set(_SYNK_FALT)
    if okanda:
        raise ValueError(f"Okända fält i synk_status: {sorted(okanda)}")
    rad = las_synk_status(jobb) or {f: None for f in _SYNK_FALT}
    rad.update(falt)
    if not rad.get("status"):
        rad["status"] = "okand"
    kolumner = ["jobb"] + _SYNK_FALT
    varden = [jobb] + [rad[f] for f in _SYNK_FALT]
    platser = ", ".join([_ph()] * len(kolumner))
    conn = _hamta_db()
    try:
        cur = conn.cursor()
        if _ar_postgres():
            uppdatera = ", ".join(f"{f} = EXCLUDED.{f}" for f in _SYNK_FALT)
            cur.execute(
                f"INSERT INTO rattspraxis.synk_status ({', '.join(kolumner)}) "
                f"VALUES ({platser}) ON CONFLICT (jobb) DO UPDATE SET {uppdatera}",
                varden,
            )
        else:
            cur.execute(
                f"INSERT OR REPLACE INTO synk_status ({', '.join(kolumner)}) "
                f"VALUES ({platser})",
                varden,
            )
        conn.commit()
    finally:
        conn.close()


def rakna_lokalt() -> dict:
    """Antal avgöranden och PDF-texter i den lokala databasen."""
    conn = _hamta_db()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {_prefix()}avgorande_cache")
        avgoranden = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {_prefix()}pdf_cache")
        pdf_texter = cur.fetchone()[0]
    finally:
        conn.close()
    return {"avgoranden": int(avgoranden), "pdf_texter": int(pdf_texter)}


# ---------------------------------------------------------------------------
# PDF-cache
# ---------------------------------------------------------------------------

def _las_pdf_cache(fillagring_id: str) -> str | None:
    """Hämtar cachad PDF-text om den finns och inte gått ut."""
    try:
        conn = _hamta_db()
        cur = conn.cursor()
        cur.execute(
            f"SELECT text_md, ttl_expires FROM {_prefix()}pdf_cache WHERE fillagring_id = {_ph()}",
            (fillagring_id,),
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
        ttl = _ttl_expires(PDF_CACHE_TTL_DAGAR)
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
