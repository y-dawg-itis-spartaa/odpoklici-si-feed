#!/usr/bin/env python3
"""Build a 90-day rolling recalls.json from official GOV.SI and JAZMP pages.

Python standard library only. The optional `image` field is the official
HTTPS image URL on gov.si or jazmp.si — this feed is text-only (no photo files).
Only collects food, non-food, medicines and medical devices (no vehicles).
Drops border / not-marketed-in-Slovenia notices, matching the v0.1 app set.
Risk colours are the scraper's own triage, not an official rating.
"""
from __future__ import annotations

import argparse
import html as htmlmod
import json
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Ljubljana")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=1), name="CET")

WINDOW_DAYS = 90
SCHEMA_VERSION = 1
GOV_LIST = (
    "https://www.gov.si/podrocja/podjetnistvo-in-gospodarstvo/"
    "varstvo-potrosnikov-in-konkurence/nevarni-in-neskladni-izdelki/"
)
JAZMP_ARHIV = "https://www.jazmp.si/arhiv-obvestil/"
JAZMP_NEUST = "https://www.jazmp.si/category/neustrezni-izdelki/"
OFFICIAL_HOSTS = {
    "www.gov.si",
    "gov.si",
    "www.jazmp.si",
    "jazmp.si",
}
FOOD_CATS = {
    "Živila",
    "Prehranska dopolnila",
    "Živila za posebne skupine",
    "Krma",
}
VEHICLE_CATS = {
    "Rekreacijska plovila",
    "Vozila",
    "Osebna vozila",
    "Motorni vozila",
}
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BYTES = 4_000_000
PAGE_SIZE = 10
MAX_LIST_PAGES = 40
MAX_JAZMP_PAGES = 20

# Phrases that mark a GOV.SI notice as border / not placed on the SI market.
BORDER_RX = re.compile(
    r"ni bilo dano v promet"
    r"|ni bila?o? dano v promet"
    r"|ni bila sproščen"
    r"|ni bilo sproščen"
    r"|ni bila?o? sproščen[aoe]? na slovenski trg"
    r"|ni dosegla potrošnikov"
    r"|zadržan[ao] ob vstopu"
    r"|ni bila sproščena v prost promet"
    r"|prepoved uvoza"
    r"|ni bilo dano v promet v sloveniji"
    r"|ni na voljo na slovenskem trgu"
    r"|uvozna pošiljka ni bila sproščen"
    r"|živilo ni bilo dano v promet"
    r"|ni bila sproščena na slovenski trg",
    re.I,
)

JAZMP_EXCLUDE_RX = re.compile(
    r"poziv veletrgovc"
    r"|vabilo na"
    r"|izobraževan"
    r"|poudarki s seje"
    r"|odbora prac"
    r"|medsafetyweek"
    r"|kampanja"
    r"|nova obrazc"
    r"|seje odbora"
    r"|farmakovigilančn\w* bilten"
    r"|izšla\s+\w+\s+številka"
    r"|priporoča\w* evrop\w* komisiji odobritev"
    r"|pozitivno mnenje za"
    r"|prednostno razvrstitev",
    re.I,
)
JAZMP_INCLUDE_RX = re.compile(
    r"varnostn"
    r"|neustrezn"
    r"|odpoklic"
    r"|umik"
    r"|ponaredek"
    r"|ponaredk"
    r"|korektivn"
    r"|kakovost"
    r"|neskladn"
    r"|obvestilo o"
    r"|pripomočk"
    r"|pripomock"
    r"|zdravil"
    r"|senzor"
    r"|infundiranj"
    r"|steriln"
    r"|fsca|fsn"
    r"|defekt"
    r"|kontrafak"
    r"|nelegaln",
    re.I,
)

RED_RX = re.compile(
    r"salmonel|listeri|e\.\s*coli|escherichia|kampilobakter|campylobacter"
    r"|aflatoksin|ohratoksin|ochratoxin|histamin"
    r"|patogen|mikrobiološk"
    r"|steklen[aio]?\b|delci stekla|kovinski delc|metal fragment"
    r"|etilenoksid|ethylene oxide"
    r"|zadušit|električn(?:i )?udarec|opeklin|electric shock|choking"
    r"|ponaredek|ponaredk|counterfeit|neodobren|nelegaln"
    r"|sterilnost|sterility|steriln"
    r"|nedovoljen|kristal vijolič"
    r"|alergen\w*[^.]{0,40}(?:ni naveden|neoznačen|ni označen|undeclared)"
    r"|(?:neoznačen|ni naveden|undeclared)[^.]{0,40}alergen",
    re.I,
)
YELLOW_RX = re.compile(
    r"neskladn[oa],?\s*a\s*varn|živilo je varno|izdelek je varen"
    r"|ocena tveganja je pokazala, da je živilo varno"
    r"|ni nevarn|only (?:a )?labelling",
    re.I,
)
BABY_RX = re.compile(r"dojenč|dojenčk|otrok|baby|infant|dummy|cucla", re.I)

LABEL_MAP = {
    "Kategorija": "catRaw",
    "Podkategorija": "sub",
    "Seznam izdelkov": "products",
    "Opis izdelka (tip/oznaka)": "desc",
    "Opis izdelka": "desc",
    "Proizvajalec": "maker",
    "Dobavitelj": "supplier",
    "Distributer": "distributor",
    "Uvoznik": "importer",
    "Vrsta tveganja": "riskType",
    "Država porekla": "origin",
    "Opis tveganja": "hazard",
    "Opis neskladnosti": "hazard",
    "Ukrep": "action",
    "Priporočila uporabniku": "advice",
    "Datum": "dateRaw",
    "Institucija": "institution",
}


def now_local() -> datetime:
    return datetime.now(TZ)


def parse_si_date(text: str) -> Optional[str]:
    if not text:
        return None
    t = htmlmod.unescape(text).replace("\xa0", " ").replace("&nbsp;", " ")
    t = re.sub(r"\s+", " ", t).strip()
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # "13. novembra 2026"
    months = {
        "januar": 1, "februar": 2, "marec": 3, "april": 4, "maj": 5, "junij": 6,
        "julij": 7, "avgust": 8, "september": 9, "oktober": 10, "november": 11,
        "december": 12,
    }
    m = re.search(
        r"(\d{1,2})\.\s*(januarja|februarja|marca|aprila|maja|junija|"
        r"julija|avgusta|septembra|oktobra|novembra|decembra)\s*(\d{4})",
        t, re.I,
    )
    if m:
        stem = m.group(2).lower()
        for k, v in months.items():
            if stem.startswith(k[:4]):
                try:
                    return date(int(m.group(3)), v, int(m.group(1))).isoformat()
                except ValueError:
                    return None
    return None


def is_challenge(html: str) -> bool:
    head = html[:4000].lower()
    return "one moment, please" in head or (
        "<title>one moment, please" in head
    )


def strip_tags(chunk: str) -> str:
    chunk = re.sub(r"<br\s*/?>", "\n", chunk, flags=re.I)
    chunk = re.sub(r"</p>\s*<p>", "\n", chunk, flags=re.I)
    chunk = re.sub(r"<[^>]+>", " ", chunk)
    chunk = htmlmod.unescape(chunk).replace("\xa0", " ")
    chunk = re.sub(r"[ \t]+", " ", chunk)
    chunk = re.sub(r" *\n *", "\n", chunk)
    return chunk.strip()


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, delay: float = 0.45):
        self.delay = delay
        self._last = 0.0
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.ok = 0
        self.fail = 0

    def get(self, url: str, retries: int = 1, binary: bool = False):
        if not url.lower().startswith("https://"):
            raise FetchError(f"refusing non-HTTPS URL: {url}")
        last_err = None
        for attempt in range(retries + 1):
            wait = self.delay if attempt == 0 else max(5.0, self.delay * 3)
            elapsed = time.time() - self._last
            if elapsed < wait:
                time.sleep(wait - elapsed)
            req = Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8",
                    "Accept-Language": "sl,en;q=0.8",
                },
            )
            try:
                with self.opener.open(req, timeout=30) as resp:
                    data = resp.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise FetchError(f"response too large: {url}")
                    ctype = resp.headers.get("Content-Type", "")
                    final = resp.geturl()
                self._last = time.time()
                if binary:
                    self.ok += 1
                    return data, final, ctype
                html = data.decode("utf-8", errors="replace")
                if is_challenge(html):
                    last_err = FetchError(f"challenge page: {url}")
                    if attempt < retries:
                        continue
                    self.fail += 1
                    raise last_err
                self.ok += 1
                return html, final, ctype
            except (HTTPError, URLError, TimeoutError, FetchError) as e:
                last_err = e
                self._last = time.time()
                if attempt < retries:
                    continue
                self.fail += 1
                raise FetchError(f"{url}: {e}") from e
        self.fail += 1
        raise FetchError(str(last_err))


def official_image_url(src: str, base: str) -> Optional[str]:
    if not src:
        return None
    src = htmlmod.unescape(src).strip()
    if src.startswith("//"):
        src = "https:" + src
    full = urljoin(base, src)
    if not full.lower().startswith("https://"):
        return None
    host = urlparse(full).hostname or ""
    if host.lower() not in OFFICIAL_HOSTS:
        return None
    path = urlparse(full).path.lower()
    if not re.search(r"\.(jpe?g|png|webp|gif)$", path):
        # GOV.SI sometimes serves resized assets without a clear type in query; path still has ext.
        if "/assets/" not in path and "/wp-content/uploads/" not in path:
            return None
    if re.search(r"/(?:logo|favicon|apple-touch)[^/]*\.(?:png|jpe?g|webp|gif|ico|svg)$", path):
        return None
    return full


def pick_gov_image(html: str, base: str, listing_thumb: Optional[str] = None) -> Optional[str]:
    candidates = []
    for m in re.finditer(
        r'(?:href|src|data-src)="(/assets/seznami/[^"]+|https://(?:www\.)?gov\.si/assets/seznami/[^"]+)"',
        html, re.I,
    ):
        u = official_image_url(m.group(1), base)
        if u:
            candidates.append(u)
    # Prefer a mid-size Fill/Scale over a 2000px FitMax, so we download less.
    def score(u: str) -> int:
        lu = u.lower()
        if "scalemaxwidthwzIw" in lu or "scalemaxwidthwzIwM" in lu:
            return 3
        if "fillwz" in lu or "__fill" in lu:
            return 2
        if "fitmax" in lu:
            return 1
        return 2
    if candidates:
        candidates.sort(key=score, reverse=True)
        return candidates[0]
    return official_image_url(listing_thumb, base) if listing_thumb else None


def parse_gov_list(html: str, base: str):
    rows = []
    for m in re.finditer(
        r'<tr>\s*<td class="td-title"[^>]*>\s*<div class="cell">\s*'
        r'<a href="([^"]+)">\s*([^<]+?)\s*</a>[\s\S]*?'
        r'<td class=" rdate"[^>]*>\s*<div class="cell">\s*<p>(.*?)</p>'
        r'[\s\S]*?(?:<img src="([^"]+)"[\s\S]*?)?</tr>',
        html, re.I,
    ):
        href, title, raw_date, thumb = m.group(1), m.group(2), m.group(3), m.group(4)
        url = urljoin(base, href)
        if "/nevarni-in-neskladni-izdelki/" not in url:
            continue
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        if not slug or slug.startswith("?") or slug == "nevarni-in-neskladni-izdelki":
            continue
        rows.append({
            "id": slug,
            "title": htmlmod.unescape(title).strip(),
            "date": parse_si_date(raw_date),
            "link": url.split("?")[0],
            "thumb": thumb,
        })
    next_start = None
    # If this page still has a "Naprej" / later start= link, caller paginates.
    starts = [int(x) for x in re.findall(r"\?start=(\d+)", html)]
    return rows, starts


def parse_gov_detail(html: str, item: dict) -> dict:
    fields = {}
    for m in re.finditer(
        r'<strong class="title">\s*([^<]+?)\s*</strong>\s*'
        r'<div class="right-content typo">([\s\S]*?)</div>',
        html,
    ):
        label = htmlmod.unescape(m.group(1)).strip()
        body = m.group(2)
        if re.search(r"image-element|rimage|<img", body, re.I) and label.lower().startswith("slika"):
            continue
        text = strip_tags(body)
        key = LABEL_MAP.get(label)
        if key and text:
            fields[key] = text
    intro = ""
    cm = re.search(
        r'<div class="content col left[^"]*">([\s\S]*?)<ul class="list registry-record-list">',
        html,
    )
    if cm:
        intro = strip_tags(re.sub(r"<h1[\s\S]*?</h1>", " ", cm.group(1), flags=re.I))
        # drop leading title repeat
        if intro.startswith(item["title"]):
            intro = intro[len(item["title"]):].strip()
    date_iso = parse_si_date(fields.get("dateRaw", "")) or item.get("date")
    cat_raw = fields.get("catRaw", "")
    if cat_raw in VEHICLE_CATS or re.search(r"vozil", cat_raw, re.I):
        item["skip"] = "vehicle"
    blob = " ".join([
        intro,
        fields.get("advice", ""),
        fields.get("action", ""),
        fields.get("hazard", ""),
    ])
    if BORDER_RX.search(blob):
        item["skip"] = "border"
    image = pick_gov_image(html, item["link"], item.get("thumb"))
    out = empty_item()
    out.update({
        "id": item["id"],
        "title": item["title"],
        "date": date_iso,
        "catRaw": cat_raw,
        "sub": fields.get("sub", ""),
        "intro": intro,
        "products": fields.get("products", ""),
        "desc": fields.get("desc", ""),
        "maker": fields.get("maker", ""),
        "supplier": fields.get("supplier", ""),
        "distributor": fields.get("distributor", ""),
        "riskType": fields.get("riskType", ""),
        "origin": fields.get("origin", ""),
        "hazard": fields.get("hazard", ""),
        "action": fields.get("action", ""),
        "advice": fields.get("advice", ""),
        "institution": fields.get("institution", ""),
        "link": item["link"],
        "source": "GOV.SI",
        "_image_src": image,
        "skip": item.get("skip"),
    })
    if fields.get("importer") and not out["supplier"]:
        out["supplier"] = fields["importer"]
    elif fields.get("importer"):
        out["supplier"] = (out["supplier"] + "\nUvoznik: " + fields["importer"]).strip()
    out["cat"] = "food" if out["catRaw"] in FOOD_CATS else "nonfood"
    out["risk"] = triage(out)
    return out


def empty_item() -> dict:
    return dict(
        id="", title="", date=None, risk="orange", cat="nonfood", catRaw="",
        sub="", intro="", products="", desc="", maker="", supplier="",
        distributor="", riskType="", origin="", hazard="", action="",
        advice="", institution="", link="", source="",
    )


def triage(it: dict) -> str:
    blob = " ".join([
        it.get("hazard", ""), it.get("riskType", ""), it.get("intro", ""),
        it.get("action", ""), it.get("title", ""), it.get("advice", ""),
        it.get("desc", ""),
    ])
    if it.get("source") == "JAZMP":
        return "red"
    if YELLOW_RX.search(blob) and not RED_RX.search(blob):
        return "yellow"
    if RED_RX.search(blob):
        return "red"
    action = (it.get("action") or "").lower()
    if "odpoklic" in action:
        return "red"
    if BABY_RX.search(blob) and it.get("cat") in {"food", "nonfood"}:
        # baby product with a stated health risk -> red; otherwise orange
        if re.search(r"tvegan|alergen|nevarn|škodo|škodljiv", blob, re.I):
            return "red"
    if "umik" in action or "kemič" in (it.get("riskType") or "").lower():
        return "orange"
    return "orange"


def classify_danger(it: dict) -> str:
    """Optional item['danger'] for the app's Danger effects. Backward compatible.
    Values: bacterial, foreign, chemical, allergen, medical, quality.
    """
    cat = (it.get("cat") or "").lower()
    src = (it.get("source") or "").lower()
    if cat in {"medicine", "device"} or src == "jazmp":
        return "medical"
    blob = " ".join([
        it.get("hazard") or "", it.get("riskType") or "", it.get("intro") or "",
        it.get("title") or "", it.get("desc") or "", it.get("action") or "",
        it.get("advice") or "", it.get("products") or "",
    ]).lower()
    if re.search(
        r"alergen|allerg|niso naveden|ni naveden|undeclared|neoznačen|neoznacen|"
        r"nedeklarir|linalol|linalool|izoeugenol",
        blob,
    ):
        return "allergen"
    if re.search(r"prisotnost (glutena|arašid|mleka|jajc|sezam|soje|orešč|mlečnih)", blob):
        return "allergen"
    # specific toxins before generic mould wording
    if re.search(
        r"aflatok|mikotoksin|fumonizin|ohratok|pesticid|pesticide|mycotoxin|"
        r"ethylene|etilen.?oksid",
        blob,
    ):
        return "chemical"
    if re.search(
        r"mikrobiološk|salmonel|lister|e\.\s*coli|escherichia|bakterij|"
        r"campylobact|monocyto|kronobact|noravirus|bacillus|plesen|plesni|"
        r"pljesni|\bmikrob",
        blob,
    ):
        return "bacterial"
    if re.search(
        r"kemič|ftalat|svinec|bisfenol|žveplov|dehp\b|glicidil|glycid|kadmi|"
        r"živo srebro|\blead\b|aromatskih amin|migracija|hlapnih snovi|"
        r"težk(?:ih|e) kovin|nitrozab|tadalafil|sildenafil|učinkovine zdravil",
        blob,
    ):
        return "chemical"
    if re.search(
        r"fizikaln|\bstekl[oia]\b|kovinsk|nečistoč|necistoc|zadušit|"
        r"majhni del|\btujek\b|kovinskih delc|foreign body|\bglass\b|fragment",
        blob,
    ):
        return "foreign"
    return "quality"


def classify_genre(it: dict) -> str:
    """Optional item['genre'] (finer reason). Backward compatible."""
    cat = (it.get("cat") or "").lower()
    dang = it.get("danger") or ""
    b = " ".join([
        it.get("hazard") or "", it.get("riskType") or "", it.get("intro") or "",
        it.get("title") or "", it.get("desc") or "", it.get("action") or "",
        it.get("advice") or "", it.get("products") or "",
    ]).lower()
    if cat == "medicine" or (dang == "medical" and cat != "device"):
        if cat == "device":
            return "device"
        if cat == "medicine" or "zdravil" in b:
            return "medicine"
    if cat == "device" or dang == "medical":
        return "device"
    if re.search(r"arašid|peanut", b) and re.search(r"niso naveden|ni naveden|neoznačen|nedeklarir|undeclared|alergen", b):
        return "peanut"
    if re.search(r"gluten|celiak", b):
        return "gluten"
    if re.search(r"alergena mleka|mlečne beljakovine|mleka, ki na embalaži", b):
        return "milk"
    if re.search(r"sojin|soje", b) and re.search(r"niso naveden|neoznačen|nedeklarir|alergen", b):
        return "soy"
    if re.search(r"linalol|linalool|limonene|cinnamal|izoeugenol|kumarin|alfa-izometil|dišavn", b):
        return "fragrance"
    if dang == "allergen":
        return "allergen_other"
    if re.search(r"lister", b):
        return "listeria"
    if re.search(r"salmonel", b):
        return "salmonella"
    if re.search(r"e\.\s*coli|escherichia|hemoragični kolitis|hemolitični uremični", b):
        return "ecoli"
    if re.search(r"noravirus", b):
        return "norovirus"
    if re.search(r"bacillus", b):
        return "bacillus"
    if re.search(r"aflatok|mikotoksin|fumonizin|ohratok|mycotoxin", b):
        return "mycotoxin"
    if re.search(r"plesen|plesni|pljesni", b):
        return "mould"
    if dang == "bacterial":
        return "microbe_other"
    if re.search(r"etilen.?oksid|ethylene oxide", b):
        return "eto"
    if re.search(r"pesticid", b):
        return "pesticide"
    if cat == "nonfood" and re.search(r"ftalat|dehp|svinec|\blead\b|nikelj|nickel", b):
        return "phthalate_lead"
    if re.search(r"svinec|kadmi|živo srebro|težk(?:ih|e) kovin|\blead\b|mercury", b):
        return "heavymetal"
    if re.search(r"bisfenol", b):
        return "bisphenol"
    if re.search(r"aromatskih amin|migracija", b):
        return "amine"
    if re.search(r"tadalafil|sildenafil|učinkovine zdravil", b):
        return "undeclared_drug"
    if re.search(r"nitrozab", b):
        return "nitrosamine"
    if re.search(r"žveplov", b):
        return "so2"
    if re.search(r"glicidil|glycid", b):
        return "glycidyl"
    if re.search(r"hlapnih snovi", b):
        return "voc"
    if re.search(r"gumbn|predal za bater|pokrov(?:a)? predala za bater|prosto dostopnost bater|dostopn\w* (?:do )?bater|dostopnih (?:gumbnih |aa )?bater|baterij\w* (?:v njem )?lahko dostop|postale dostopne gumbne", b):
        return "battery"
    if re.search(r"\bstekl[oia]\b|\bglass\b", b):
        return "glass"
    if re.search(r"kovinsk|kovinskih delc|metal(?:nih|ni)?\s*delc", b):
        return "metal"
    if re.search(r"zadušit|majhni del|majhnih delc", b):
        return "choke"
    if re.search(r"plast|vrečk", b) and dang == "foreign":
        return "plastic"
    if re.search(r"nečistoč|fizikaln", b):
        return "impurity"
    if dang == "foreign":
        return "foreign_other"
    if re.search(r"okvara vida|mrežnic|bliskavic", b):
        return "vision"
    if re.search(r"požar|pregrevan", b):
        return "fire"
    if re.search(r"električni udar|napajaln|kabel|električnih varnost", b):
        return "electrical"
    if re.search(r"označen|označene|označeno|prenizka glede|previsoko količino vitamina", b):
        return "labelling"
    if dang == "quality":
        return "quality_other"
    return "other"


def parse_jazmp_list(html: str):
    posts = []
    parts = re.split(r"<article\b", html, flags=re.I)
    for part in parts[1:]:
        href_m = re.search(
            r'href="(https://(?:www\.)?jazmp\.si/(20\d{2})/(\d{2})/(\d{2})/[^"]+)"',
            part,
        )
        if not href_m:
            continue
        url = href_m.group(1)
        iso = f"{href_m.group(2)}-{href_m.group(3)}-{href_m.group(4)}"
        title_m = re.search(r'rel="bookmark"[^>]*>([\s\S]*?)</a>', part)
        title = strip_tags(title_m.group(1)) if title_m else url.rstrip("/").split("/")[-1]
        cats = re.findall(r"category-([a-z0-9-]+)", part)
        pub = re.search(r'itemprop="datePublished">([\s\S]*?)</', part)
        if pub:
            iso = parse_si_date(pub.group(1)) or iso
        posts.append({"title": title, "link": url, "date": iso, "cats": cats})
    # next page
    next_url = None
    m = re.search(r'<a[^>]+class="[^"]*next[^"]*"[^>]+href="([^"]+)"', html, re.I)
    if not m:
        m = re.search(r'href="(https://(?:www\.)?jazmp\.si/[^"]+/page/\d+/)"[^>]*>\s*Naslednj', html, re.I)
    if m:
        next_url = m.group(1)
    return posts, next_url


def jazmp_relevant(title: str, cats: list[str]) -> bool:
    blob = title + " " + " ".join(cats)
    if JAZMP_EXCLUDE_RX.search(blob):
        return False
    if JAZMP_INCLUDE_RX.search(blob):
        return True
    # category-level safety buckets
    if any(c in {
        "neustrezni-izdelki",
        "vigilanca-medicinskih-pripomockov",
        "medicinski-pripomocki",
    } for c in cats) and "obvestila-za-javnost" in cats:
        return True
    return False


def jazmp_cat(title: str, cats: list[str]) -> str:
    blob = (title + " " + " ".join(cats)).lower()
    if any(c in {
        "medicinski-pripomocki",
        "vigilanca-medicinskih-pripomockov",
    } for c in cats):
        return "device"
    if re.search(r"pripomočk|pripomock|senzor|aplikacij|adapter|kondom|kanila", blob):
        return "device"
    return "medicine"


def parse_jazmp_detail(html: str, post: dict) -> dict:
    content = ""
    m = re.search(
        r'<div class="entry-content[^"]*"[^>]*>([\s\S]*?)</div>\s*(?:<footer|<!--|\Z)',
        html, re.I,
    )
    if m:
        content = strip_tags(m.group(1))
        # cut related-posts / cookie noise
        content = re.split(r"\nPravno obvestilo|\nPiškot", content)[0].strip()
        if len(content) > 1200:
            content = content[:1197] + "…"
    img = None
    for pat in (
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
        r'<img[^>]+class="[^"]*wp-post-image[^"]*"[^>]+src="([^"]+)"',
        r'<img[^>]+src="(https://(?:www\.)?jazmp\.si/wp-content/uploads/[^"]+)"',
    ):
        im = re.search(pat, html, re.I)
        if im:
            img = official_image_url(im.group(1), post["link"])
            if img:
                break
    cat = jazmp_cat(post["title"], post.get("cats") or [])
    out = empty_item()
    out.update({
        "id": "jazmp-" + post["link"].rstrip("/").split("/")[-1][:40],
        "title": post["title"],
        "date": post["date"],
        "risk": "red",
        "cat": cat,
        "catRaw": "Zdravila" if cat == "medicine" else "Medicinski pripomočki",
        "intro": "",
        "hazard": content,
        "institution": "JAZMP",
        "link": post["link"],
        "source": "JAZMP",
        "noteEn": False,
        "_image_src": img,
    })
    return out


def collect_gov(fetcher: Fetcher, cutoff: date) -> tuple[list[dict], int]:
    items = []
    skipped_border = 0
    start = 0
    seen = set()
    for _page in range(MAX_LIST_PAGES):
        url = GOV_LIST if start == 0 else f"{GOV_LIST}?start={start}"
        html, final, _ = fetcher.get(url, retries=1)
        rows, _starts = parse_gov_list(html, final)
        if not rows:
            print(f"GOV.SI page start={start}: 0 rows, stopping")
            break
        dates = [r["date"] for r in rows if r["date"]]
        print(f"GOV.SI page start={start}: {len(rows)} rows, {dates[0] if dates else '?'} … {dates[-1] if dates else '?'}")
        all_old = True
        for row in rows:
            if not row["date"]:
                all_old = False
                continue
            if date.fromisoformat(row["date"]) < cutoff:
                continue
            all_old = False
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            try:
                dhtml, dfinal, _ = fetcher.get(row["link"], retries=1)
            except FetchError as e:
                print(f"  detail fail {row['id']}: {e}", file=sys.stderr)
                continue
            row["link"] = dfinal.split("?")[0]
            parsed = parse_gov_detail(dhtml, row)
            if parsed.get("skip") == "border":
                skipped_border += 1
                continue
            if parsed.get("skip") == "vehicle":
                continue
            parsed.pop("skip", None)
            items.append(parsed)
        if all_old:
            break
        start += PAGE_SIZE
    return items, skipped_border


def collect_jazmp(fetcher: Fetcher, cutoff: date) -> list[dict]:
    items = []
    seen = set()
    for origin in (JAZMP_ARHIV, JAZMP_NEUST):
        url = origin
        for _page in range(MAX_JAZMP_PAGES):
            if not url:
                break
            try:
                html, final, _ = fetcher.get(url, retries=1)
            except FetchError as e:
                print(f"JAZMP list fail {url}: {e}", file=sys.stderr)
                break
            posts, next_url = parse_jazmp_list(html)
            print(f"JAZMP {final}: {len(posts)} posts")
            if not posts:
                break
            all_old = True
            for post in posts:
                if not post["date"] or date.fromisoformat(post["date"]) < cutoff:
                    continue
                all_old = False
                if not jazmp_relevant(post["title"], post.get("cats") or []):
                    continue
                key = post["link"].rstrip("/").split("/")[-1]
                if key in seen:
                    continue
                seen.add(key)
                try:
                    dhtml, dfinal, _ = fetcher.get(post["link"], retries=1)
                    post["link"] = dfinal.split("?")[0]
                    items.append(parse_jazmp_detail(dhtml, post))
                except FetchError as e:
                    print(f"  JAZMP detail fail {post['title'][:60]}: {e}", file=sys.stderr)
                    # still include listing-level data so a WAF blip doesn't drop the notice
                    stub = parse_jazmp_detail("", post)
                    items.append(stub)
            if all_old:
                break
            url = next_url
    return items


def attach_official_images(items: list[dict]) -> int:
    """Set item['image'] to the official HTTPS URL when the host is allow-listed. No files are written."""
    n = 0
    for it in items:
        src = it.pop("_image_src", None)
        host = (urlparse(src).hostname or "").lower() if src else ""
        if src and src.lower().startswith("https://") and host in OFFICIAL_HOSTS:
            it["image"] = src
            n += 1
        # leave 'image' absent when there is no official photo
    return n


def dedupe(items: list[dict]) -> list[dict]:
    out, seen = [], set()
    for it in items:
        key = (it.get("source"), it.get("id"))
        link = it.get("link", "").rstrip("/")
        if key in seen or link in seen:
            continue
        seen.add(key)
        seen.add(link)
        out.append(it)
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape official SI recalls into recalls.json")
    ap.add_argument("--out", default="recalls.json")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--delay", type=float, default=0.45)
    args = ap.parse_args()

    today = now_local().date()
    cutoff = today - timedelta(days=args.days)
    print(f"Window: {cutoff.isoformat()} … {today.isoformat()} ({args.days} days)")

    fetcher = Fetcher(delay=args.delay)
    gov, skipped_border = collect_gov(fetcher, cutoff)
    jazmp = collect_jazmp(fetcher, cutoff)
    items = dedupe(gov + jazmp)
    n_img = attach_official_images(items)

    # drop any leftover internal keys
    for it in items:
        it.pop("_image_src", None)
        it.pop("skip", None)
        it["danger"] = classify_danger(it)
        it["genre"] = classify_genre(it)

    dates = [it["date"] for it in items if it.get("date")]
    generated = now_local().replace(microsecond=0)
    doc = {
        "version": 1,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated.isoformat(),
        "dataDate": today.isoformat(),
        "window": f"{cutoff.isoformat()}–{today.isoformat()}",
        "windowDays": args.days,
        "report": "live-scrape",
        "officialUrl": GOV_LIST,
        "items": items,
    }
    out = Path(args.out)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    src = Counter(it["source"] for it in items)
    cat = Counter(it["cat"] for it in items)
    risk = Counter(it["risk"] for it in items)
    danger = Counter(it.get("danger") or "—" for it in items)
    genre = Counter(it.get("genre") or "—" for it in items)
    print("---")
    hosts = Counter(
        (urlparse(it["image"]).hostname or "").lower()
        for it in items if it.get("image")
    )
    print(f"Wrote {out}  items={len(items)}  images={n_img}  border_dropped={skipped_border}")
    print(f"sources: {dict(src)}")
    print(f"cats: {dict(cat)}")
    print(f"risk: {dict(risk)}")
    print(f"danger: {dict(danger)}")
    print(f"genre: {dict(genre)}")
    print(f"image hosts: {dict(hosts)}")
    print(f"newest: {max(dates) if dates else '—'}  oldest: {min(dates) if dates else '—'}")
    print(f"http ok={fetcher.ok} fail={fetcher.fail}")
    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main())
