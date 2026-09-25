#!/usr/bin/env python3
"""
Textextraktion ur Domstolsverkets PDF-bilagor.

Används av både MCP-servern (hamta_pdf) och synkskriptet, så att texten som
lagras i pdf_cache blir densamma oavsett vem som hämtade PDF:en.

Själva extraktionen sker i pdftext_skydd.extrahera_pdf, som kör den i en
egen process under minnes- och tidsvakt och sätter rätt OCR-språk. Se den
modulens dokumentation för miljövariablerna som styr vakten och OCR-kön
(RP_OCR_SPRAK, RP_PDF_MAX_MINNE_MB, RP_PDF_TIDSGRANS_S, RP_PDF_SIDBLOCK,
RP_OCR_KO_MAPP).
"""

import logging
import threading

from pdftext_skydd import extrahera_pdf

log = logging.getLogger(__name__)

# Miljövariabelprefix och standardspråk för extrahera_pdf.
PREFIX = "RP"
STANDARDSPRAK = "swe+eng"

# PyMuPDF är inte trådsäkert, och MCP-verktygen körs på arbetstrådar. Låset
# serialiserar extraktionen.
_pdf_las = threading.Lock()


class PdfFel(RuntimeError):
    """PDF:en gick inte att läsa."""


def extrahera_text(pdf_bytes: bytes, *, kalla_id: str, kalla_url: str = "") -> str:
    """
    Extraherar PDF:ens text som markdown. Kastar PdfFel om det inte går.

    kalla_id och kalla_url identifierar dokumentet i OCR-kön (ocr_ko/ko.jsonl)
    om någon sida saknar textlager eller om ett sidblock fick läsas med ren
    textutvinning.
    """
    try:
        with _pdf_las:
            resultat = extrahera_pdf(
                pdf_bytes, prefix=PREFIX, standardsprak=STANDARDSPRAK,
                kalla_id=kalla_id, kalla_url=kalla_url,
            )
    except Exception as e:
        raise PdfFel(
            f"Texten i PDF:en kunde inte extraheras ({e}). Filen kan vara "
            "skadad eller bara innehålla inskannade bilder."
        ) from e

    if resultat.i_ocr_ko:
        log.info(
            "PDF %s lagd i OCR-kön (metod=%s, sidor utan textlager=%s): %s",
            kalla_id, resultat.metod, resultat.sidor_utan_textlager, resultat.orsak,
        )
    return resultat.text
