#!/usr/bin/env python3
"""
Databaslager för rättspraxis-MCP-servern.

Hanterar anslutning till PostgreSQL eller SQLite, schemaskapande och
cache-operationer för avgöranden och PDF-texter.
Konfiguration läses från .env via DATABASE_URL.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

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
    parsed = urlparse(DATABASE_URL)
    db_fil = parsed.path.lstrip("/") if DATABASE_URL.startswith("sqlite:///") else DATABASE_URL
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

    Baseline: v1.0.0 (2026-05-11)
    Migrationer: (inga ännu)
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
        # Migrationer — lägg nya ALTER TABLE IF NOT EXISTS-block här.
        # (inga migrationer ännu)
        # ---------------------------------------------------------------

        conn.commit()
        conn.close()
        log.info("Databasschema verifierat")
    except Exception as e:
        log.warning("Kunde inte säkerställa databasschema: %s", e)


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


def _skriv_avgorande_cache(a: dict):
    """Lagrar ett avgörande i cachen."""
    try:
        conn = _hamta_db()
        cur = conn.cursor()
        ttl = _ttl_expires(METADATA_CACHE_TTL_DAGAR)
        avgorande_id = a.get("id", "")
        domstolkod = (a.get("domstol") or {}).get("domstolKod")
        avgorandedatum = a.get("avgorandedatum")
        vagledande = ar_vagledande(a)
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
            """, (avgorande_id, domstolkod, avgorandedatum, vagledande, benamning,
                  json.dumps(a, ensure_ascii=False), nu, ttl.isoformat()))
        else:
            cur.execute("""
                INSERT OR REPLACE INTO avgorande_cache
                    (id, domstolkod, avgorandedatum, ar_vagledande, benamning, data, hamtat, ttl_expires)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (avgorande_id, domstolkod, avgorandedatum,
                  None if vagledande is None else int(vagledande), benamning,
                  json.dumps(a, ensure_ascii=False), nu, ttl.isoformat()))

        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Fel vid skrivning till avgorande_cache: %s", e)


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
