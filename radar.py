#!/usr/bin/env python3
"""Audition Radar: collect -> OCR -> classify -> dedupe -> store -> email."""
import os, re, io, sys, json, hashlib, smtplib, difflib, argparse, datetime as dt
from email.mime.text import MIMEText
import requests, yaml
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime.now(IST)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
log = lambda *a: print(f"[{dt.datetime.now(IST):%H:%M:%S}]", *a, flush=True)

def load(p, default):
    try: return json.load(open(os.path.join(ROOT, p), encoding="utf-8"))
    except Exception: return default
def save(p, obj):
    os.makedirs(os.path.dirname(os.path.join(ROOT, p)), exist_ok=True)
    json.dump(obj, open(os.path.join(ROOT, p), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# ---------- collectors: each returns list of raw posts ----------
def col_instagram(cfg):
    token = os.getenv("APIFY_TOKEN")
    if not token: raise RuntimeError("APIFY_TOKEN not set")
    s = cfg["sources"]
    body = {"directUrls": [f"https://www.instagram.com/{h}/" for h in s["instagram"]],
            "resultsType": "posts", "resultsLimit": s.get("instagram_results_limit", 10),
            "onlyPostsNewerThan": f"{cfg['scan']['lookback_days'] + 1} days"}
    r = requests.post(f"https://api.apify.com/v2/acts/{s['apify_actor']}/run-sync-get-dataset-items",
                      params={"token": token}, json=body, timeout=290)
    r.raise_for_status()
    out, seen_handles = [], set()
    for it in r.json():
        if it.get("error"): continue
        imgs = [it.get("displayUrl")] + [c.get("displayUrl") for c in (it.get("childPosts") or [])] + list(it.get("images") or [])
        imgs = [i for i in dict.fromkeys(imgs) if i]
        seen_handles.add(it.get("ownerUsername"))
        out.append(dict(source="instagram:" + str(it.get("ownerUsername")), url=it.get("url") or f"https://www.instagram.com/p/{it.get('shortCode')}/",
                        text=it.get("caption") or "", date=it.get("timestamp"), images=imgs[:6]))
    log("instagram: handles with data:", sorted(h for h in seen_handles if h))
    return out

def col_search(cfg):
    out, errs = [], 0
    for q in cfg["sources"].get("search_queries", []):
        try:
            r = requests.post("https://html.duckduckgo.com/html/", data={"q": q}, headers=UA, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            res = soup.select(".result")
            if not res and "anomaly" in r.text.lower(): raise RuntimeError("search engine blocked (captcha); not bypassing")
            for d in res[:10]:
                a = d.select_one("a.result__a"); sn = d.select_one(".result__snippet")
                if a: out.append(dict(source="search", url=requests.utils.unquote(a["href"].split("uddg=")[-1].split("&")[0]) if "uddg=" in a["href"] else a["href"],
                                      text=(a.get_text(" ") + ". " + (sn.get_text(" ") if sn else "")), date=None, images=[]))
        except Exception as e:
            errs += 1; log("search query failed:", q, e)
    if errs and errs == len(cfg["sources"].get("search_queries", [])): raise RuntimeError("all search queries failed")
    return out

def col_web(cfg):
    out = []
    kw = re.compile(r"casting|audition|ഓഡിഷൻ|കാസ്റ്റിംഗ്", re.I)
    for p in cfg["sources"].get("web_pages", []):
        r = requests.get(p["url"], headers=UA, timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True)[:300]:
            if kw.search(a.get_text(" ")):
                link = requests.compat.urljoin(p["url"], a["href"])
                try:
                    pr = requests.get(link, headers=UA, timeout=30); ps = BeautifulSoup(pr.text, "html.parser")
                    og = ps.find("meta", property="og:image")
                    out.append(dict(source="web:" + p["name"], url=link, text=ps.get_text(" ", strip=True)[:2500], date=None, images=[og["content"]] if og else []))
                except Exception as e: log("web item failed", link, e)
    return out

COLLECTORS = {"instagram": col_instagram, "search": col_search, "web": col_web}

# ---------- OCR + poster ----------
def ocr_and_poster(post, oid):
    try:
        from PIL import Image; import pytesseract
    except Exception: return "", None
    texts, poster = [], None
    for i, u in enumerate(post["images"]):
        try:
            im = Image.open(io.BytesIO(requests.get(u, headers=UA, timeout=30).content)).convert("RGB")
            texts.append(pytesseract.image_to_string(im, lang="eng+mal"))
            if i == 0:
                im.thumbnail((640, 640)); os.makedirs(f"{ROOT}/docs/posters", exist_ok=True)
                im.save(f"{ROOT}/docs/posters/{oid}.jpg", quality=70); poster = f"posters/{oid}.jpg"
        except Exception as e: log("ocr/image failed:", str(e)[:80])
    return "\n".join(texts), poster

# ---------- classification ----------
def child_age():
    dob = os.getenv("CHILD_DOB")  # YYYY-MM-DD, secret
    if not dob: return None
    return (NOW.date() - dt.date.fromisoformat(dob)).days / 365.25

MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
def find_deadline(t):
    t = t.lower(); found = []
    for d, m in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/]*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", t):
        found.append((int(d), MON[m]))
    for m, d in re.findall(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t):
        found.append((int(d), MON[m]))
    for d, m in re.findall(r"\b(\d{1,2})[/.](\d{1,2})[/.](?:20)?\d{2}\b", t): found.append((int(d), int(m)))
    ds = []
    for d, m, y in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/,]*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s,]*(20\d\d)\b", t):
        try: ds.append(dt.date(int(y), MON[m], int(d)))
        except ValueError: pass
    if ds: return max(ds)
    for d, m in found:
        try:
            x = dt.date(NOW.year, m, d)
            if x < NOW.date() - dt.timedelta(days=180): x = dt.date(NOW.year + 1, m, d)
            ds.append(x)
        except ValueError: pass
    return max(ds) if ds else None

def ages_in(t):
    out = []
    for a, b, u in re.findall(r"(\d{1,2}(?:\.\d)?)\s*(?:-|–|to|മുതൽ)\s*(\d{1,2}(?:\.\d)?)\s*(years?|yrs?|yr|y\b|months?|mos?|വയസ്സ്?)", t, re.I):
        f = 1/12 if u.lower().startswith(("mo", "month")) else 1
        out.append((float(a) * f, float(b) * f))
    for a, b in re.findall(r"age[^\d\n]{0,20}(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})", t, re.I): out.append((float(a), float(b)))
    return out

def classify(text, cfg):
    c = cfg["criteria"]; t = text.lower(); why = []
    has = lambda ws: [w for w in ws if w.lower() in t]
    if not has(c["casting_signals"]): return "REJECTED", "No casting/audition signal in text", {}
    ex = has(c["exclude"])
    dl = find_deadline(text)
    info = {"deadline": dl.isoformat() if dl else None}
    if dl and dl < NOW.date(): return "REJECTED", f"Expired (deadline {dl})", info
    if ex and not re.search(r"\b(child|kid|baby|toddler)\b", t): return "REJECTED", f"Promotional/class ('{ex[0]}')", info
    if ex: return "NEEDS REVIEW", f"Mentions '{ex[0]}' but also kids casting", info
    kid = bool(has(c["kid_words"]))
    boys_only = re.search(r"\bboys?\b", t) and not re.search(r"girl|female|boys? and girls?|kids|children|പെൺ", t)
    girl = bool(re.search(r"\bgirls?\b|female|പെൺ", t)); anyg = bool(re.search(r"boys? (and|&|/) girls?|kids|children", t))
    gender = "male" if boys_only else "female" if girl else "any" if anyg else "unknown"
    if gender == "male": return "REJECTED", "Boys only", info
    rngs = ages_in(text); age = child_age(); age_ok = None
    if rngs and age is not None:
        age_ok = any(a - 0.01 <= age <= b + 0.99 for a, b in rngs)   # '1-3' covers up to 3y11m
    elif rngs:
        age_ok = any(a <= c["age_max"] and b >= c["age_min"] for a, b in rngs)
    if rngs and not age_ok and not kid: return "REJECTED", "Age requirement does not fit", info
    if rngs and not age_ok: return "NEEDS REVIEW", "Kid role mentioned but stated ages may not fit", info
    loc_hit = has(c["locations"]); other = has(c["other_places"])
    if not loc_hit and other: return "REJECTED", f"Location only {other[0]}", info
    types = has(c["include_types"])
    if not kid and age_ok is None: return "REJECTED", "No child requirement found", info
    problems = []
    if age_ok is None: problems.append("age unclear")
    if not loc_hit: problems.append("location unclear")
    if gender == "unknown": problems.append("gender unclear")
    if not types: problems.append("production type unclear")
    info.update(gender=gender, location=loc_hit[0] if loc_hit else None, type=types[0] if types else None)
    if problems: return "NEEDS REVIEW", "Possibly relevant but " + ", ".join(problems), info
    return "MATCH", f"Kids casting; age fits; location '{loc_hit[0]}'; type '{types[0]}'; gender {gender}", info

def norm(t): return re.sub(r"[^a-z0-9\u0d00-\u0d7f ]", "", re.sub(r"\s+", " ", t.lower()))[:600]
def contact(t):
    m = re.findall(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}|[\w.+-]+@[\w-]+\.[\w.]+|wa\.me/\d+", t)
    return ", ".join(dict.fromkeys(m)) or None

# ---------- email ----------
def send_mail(subject, html):
    u, p, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO") or os.getenv("SMTP_USER")
    if not (u and p): log("SMTP not configured; skipping email:", subject); return False
    m = MIMEText(html, "html", "utf-8"); m["Subject"], m["From"], m["To"] = subject, u, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s: s.login(u, p); s.send_message(m)
    return True
def card(o):
    e = lambda x: (x or "—").replace("<", "&lt;")
    return (f"<p><b>{e(o['title'])}</b> [{o['class']}]<br>Why: {e(o['reason'])}<br>Deadline: {e(o['info'].get('deadline'))} | Apply: {e(o.get('contact'))}"
            f"<br><a href='{o['urls'][0]}'>Original post</a><br><small>{e(o['text'][:400])}</small></p><hr>")

# ---------- main ----------
def run(sample=False, lookback=None):
    cfg = yaml.safe_load(open(f"{ROOT}/config.yaml", encoding="utf-8"))
    if lookback: cfg["scan"]["lookback_days"] = int(lookback)
    db = load("data/db.json", {"opps": {}, "scans": [], "last_failure_email": None})
    scan = {"time": NOW.isoformat(), "sources": {}, "new": 0}
    posts = []
    if sample:
        posts = load("samples/sample_posts.json", []); scan["sources"]["sample"] = {"ok": True, "count": len(posts)}
    else:
        for name, fn in COLLECTORS.items():
            if name == "instagram" and not cfg["sources"].get("instagram"): continue
            if name == "web" and not cfg["sources"].get("web_pages"): continue
            try: got = fn(cfg); posts += got; scan["sources"][name] = {"ok": True, "count": len(got)}; log(name, "ok", len(got))
            except Exception as e: scan["sources"][name] = {"ok": False, "error": str(e)[:300]}; log(name, "FAILED", e)
    cutoff = NOW - dt.timedelta(days=cfg["scan"]["lookback_days"] + 1)
    for p in posts:
        try:
            if p.get("date") and dt.datetime.fromisoformat(str(p["date"]).replace("Z", "+00:00")) < cutoff: continue
        except Exception: pass
        oid = hashlib.sha1(p["url"].split("?")[0].encode()).hexdigest()[:12]
        ocr, poster = ("", None) if sample else ocr_and_poster(p, oid)
        full = (p["text"] + "\n" + ocr + "\n" + (p.get("ocr_text") or "")).strip()
        cls, reason, info = classify(full, cfg)
        key = norm(full)
        dup = next((k for k, o in db["opps"].items() if k == oid or difflib.SequenceMatcher(None, o["key"], key).ratio() > 0.85), None)
        if dup:
            o = db["opps"][dup]; o["last_seen"] = NOW.isoformat()
            if p["url"] not in o["urls"]: o["urls"].append(p["url"])
            if p["source"] not in o["sources"]: o["sources"].append(p["source"])
            o.update({"class": cls, "reason": reason, "info": {**o["info"], **info}}); o["scans"] = o.get("scans", 0) + 1
            continue
        title = re.sub(r"\s+", " ", p["text"]).strip()[:90] or p["url"]
        db["opps"][oid] = dict(id=oid, title=title, text=full[:3000], key=key, urls=[p["url"]], sources=[p["source"]], published=p.get("date"),
                               poster=poster or (p["images"][0] if p["images"] else None), first_seen=NOW.isoformat(), last_seen=NOW.isoformat(),
                               **{"class": cls}, reason=reason, info=info, contact=contact(full), notified_class=None, scans=1)
        scan["new"] += 1
    keep = NOW - dt.timedelta(days=cfg["scan"]["retention_days"])
    for k in [k for k, o in db["opps"].items() if dt.datetime.fromisoformat(o["first_seen"]) < keep]:
        try: os.remove(f"{ROOT}/docs/posters/{k}.jpg")
        except OSError: pass
        del db["opps"][k]
    # notifications
    fresh = [o for o in db["opps"].values() if o["class"] == "MATCH" and o["notified_class"] != "MATCH"]
    if cfg["email"]["include_needs_review"]:
        fresh_r = [o for o in db["opps"].values() if o["class"] == "NEEDS REVIEW" and o["notified_class"] is None]
    else: fresh_r = []
    scan["emailed"] = False
    if fresh or fresh_r:
        html = "<h3>New matches</h3>" + ("".join(map(card, fresh)) or "<p>None</p>") + ("<h3>Needs review</h3>" + "".join(map(card, fresh_r)) if fresh_r else "")
        try:
            if send_mail(f"Audition Radar: {len(fresh)} match(es), {len(fresh_r)} to review", html):
                for o in fresh + fresh_r: o["notified_class"] = o["class"]
                scan["emailed"] = True
        except Exception as e: scan["email_error"] = str(e)[:200]; log("email failed", e)
    failed = {k: v for k, v in scan["sources"].items() if not v["ok"]}
    ig_down = "instagram" in failed
    if ig_down or len(failed) == len(scan["sources"]):
        today = NOW.date().isoformat()
        if db.get("last_failure_email") != today:
            try:
                if send_mail("Audition Radar: collection FAILURE", "<pre>" + json.dumps(failed, indent=1) + "</pre><p>Check GitHub Actions logs.</p>"): db["last_failure_email"] = today
            except Exception as e: log("failure email failed", e)
    scan["health"] = "failed" if len(failed) == len(scan["sources"]) else "degraded" if failed else "ok"
    db["scans"] = (db["scans"] + [scan])[-200:]
    save("data/db.json", db)
    pub = {"generated": NOW.isoformat(), "scans": db["scans"][-30:],
           "opps": [{k: v for k, v in o.items() if k != "key"} for o in sorted(db["opps"].values(), key=lambda o: o["first_seen"], reverse=True)]}
    save("docs/data.json", pub)
    log("done:", scan["health"], "new:", scan["new"])
    return 1 if scan["health"] == "failed" else 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--sample", action="store_true"); ap.add_argument("--lookback")
    a = ap.parse_args(); sys.exit(run(a.sample, a.lookback))
