"""EDGAR access: polite fetches (declared User-Agent, minimum gap, backoff on 403/429/503), the latest-filings
Atom feed, the daily form index, full submissions (cached gzipped), company tickers and per-CIK submissions."""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger("tender_bot")

ATOM = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-I&company=&dateb="
        "&owner=include&start={start}&count=100&output=atom")
DAILY = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
FULL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{acc}.txt"
TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FORMS = ("SC TO-I", "SC TO-I/A")
_NS = {"a": "http://www.w3.org/2005/Atom"}


class Edgar:
    def __init__(self, user_agent: str, cache_dir: str | Path, min_gap: float = 0.5, opener=None):
        if "@" not in user_agent:
            raise ValueError("SEC requires a User-Agent with a contact e-mail: set user_agent in the config "
                             "or $TENDER_BOT_USER_AGENT, e.g. \"Jane Doe jane@example.com\"")
        self.ua, self.min_gap = user_agent, float(min_gap)
        self.cache = Path(cache_dir)
        self._last = 0.0
        self._open = opener or urllib.request.urlopen

    def get(self, url: str, tries: int = 4, missing: tuple = (404,)) -> bytes | None:
        """Bytes at ``url``; None for a status in ``missing``. Raises after ``tries`` failures."""
        err = None
        for k in range(tries):
            wait = self._last + self.min_gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip"})
                with self._open(req, timeout=60) as r:
                    b = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        b = gzip.decompress(b)
                return b
            except urllib.error.HTTPError as e:
                if e.code in missing:
                    return None
                err = f"HTTP {e.code}"
                time.sleep(10 * (k + 1) if e.code in (403, 429, 503) else 2)
            except Exception as e:  # noqa: BLE001 - network errors are retried
                err = repr(e)
                time.sleep(3 * (k + 1))
        raise RuntimeError(f"EDGAR fetch failed ({err}): {url}")

    # --- discovery ----------------------------------------------------------------------------------------
    def latest(self) -> list[dict]:
        """SC TO-I and SC TO-I/A filings in the latest-filings feed (newest first), with acceptance times."""
        b = self.get(ATOM.format(start=0))
        return parse_atom(b or b"")

    def daily(self, d: date) -> list[dict]:
        """SC TO-I(/A) filings in the daily form index for ``d`` ([] before it's published or on holidays).
        EDGAR answers 403, not 404, for an index that doesn't exist yet, so 403 counts as "not published"."""
        b = self.get(DAILY.format(y=d.year, q=(d.month - 1) // 3 + 1, ymd=d.strftime("%Y%m%d")), tries=2,
                     missing=(403, 404))
        return parse_daily_index(b.decode("latin-1") if b else "")

    def submissions(self, cik: int) -> list[dict]:
        """Recent filings of ``cik`` (form, accession, filing date, acceptance time), newest first."""
        b = self.get(SUBMISSIONS.format(cik=int(cik)))
        if not b:
            return []
        r = json.loads(b).get("filings", {}).get("recent", {})
        keys = ("form", "accessionNumber", "filingDate", "acceptanceDateTime")
        rows = [dict(zip(keys, v)) for v in zip(*(r.get(k, []) for k in keys))]
        return [dict(form=x["form"], acc=x["accessionNumber"], cik=int(cik), filed=x["filingDate"],
                     accepted=_iso(x.get("acceptanceDateTime"))) for x in rows]

    # --- documents ----------------------------------------------------------------------------------------
    def full_text_path(self, cik: int, acc: str) -> Path:
        """Path of the gzipped full submission (downloaded once)."""
        p = self.cache / "filings" / acc / "full.txt.gz"
        if not p.exists():
            b = self.get(FULL.format(cik=int(cik), nodash=acc.replace("-", ""), acc=acc))
            if b is None:
                raise FileNotFoundError(f"no submission text for {acc}")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".part")
            tmp.write_bytes(gzip.compress(b))
            tmp.replace(p)
        return p

    def tickers(self, max_age_s: float = 86400) -> dict[int, list[tuple[str, str]]]:
        """``{cik: [(ticker, exchange), ...]}`` from SEC's company_tickers_exchange.json (cached a day)."""
        p = self.cache / "company_tickers_exchange.json"
        if not p.exists() or time.time() - p.stat().st_mtime > max_age_s:
            b = self.get(TICKERS)
            if b:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.with_suffix(".part").write_bytes(b)
                p.with_suffix(".part").replace(p)
        if not p.exists():
            return {}
        j = json.loads(p.read_text())
        f = j["fields"]
        out: dict[int, list] = {}
        for row in j["data"]:
            r = dict(zip(f, row))
            out.setdefault(int(r["cik"]), []).append((str(r["ticker"]).upper(), str(r.get("exchange") or "")))
        return out


def _iso(s) -> str | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def parse_atom(b: bytes) -> list[dict]:
    if not b.strip():
        return []
    root = ElementTree.fromstring(b)
    out = []
    for e in root.findall("a:entry", _NS):
        cat = e.find("a:category", _NS)
        form = cat.get("term") if cat is not None else ""
        if form not in FORMS:
            continue
        title = e.findtext("a:title", "", _NS)
        m = re.search(r"\((\d{10})\)", title)
        acc = re.search(r"accession-number=([\d-]+)", e.findtext("a:id", "", _NS))
        summary = e.findtext("a:summary", "", _NS)
        filed = re.search(r"Filed:</b>\s*(\d{4}-\d{2}-\d{2})", summary) or re.search(r"(\d{4}-\d{2}-\d{2})", summary)
        if not (m and acc):
            continue
        name = re.sub(r"^\S+(?:\s\S+)?\s-\s", "", title).rsplit(" (", 2)[0].strip()
        role = "subject" if "(Subject)" in title else "filer" if "(Filer)" in title else ""
        out.append(dict(form=form, acc=acc.group(1), cik=int(m.group(1)), name=name, role=role,
                        filed=filed.group(1) if filed else None, accepted=_iso(e.findtext("a:updated", "", _NS))))
    return prefer_subject(out)


def prefer_subject(rows: list[dict]) -> list[dict]:
    """One row per accession; when a filing is listed for both its filer and its subject, keep the subject."""
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["acc"])
        if cur is None or (r.get("role") == "subject" and cur.get("role") != "subject"):
            best[r["acc"]] = r
    return [r for r in rows if best.get(r["acc"]) is r]


def parse_daily_index(text: str) -> list[dict]:
    """form.YYYYMMDD.idx: fixed-width rows ``FORM  COMPANY  CIK  DATE  edgar/data/CIK/ACC.txt``."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(SC TO-I(?:/A)?)\s{2,}(.+?)\s{2,}(\d+)\s+(\d{8})\s+(edgar/data/\S+)", line)
        if not m:
            continue
        acc = re.search(r"(\d{10}-\d{2}-\d{6})", m.group(5))
        if acc:
            d = m.group(4)
            out.append(dict(form=m.group(1), acc=acc.group(1), cik=int(m.group(3)), name=m.group(2).strip(),
                            filed=f"{d[:4]}-{d[4:6]}-{d[6:]}", accepted=None))
    return out
