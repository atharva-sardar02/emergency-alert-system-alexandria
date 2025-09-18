# --- .env loader (paste near top of reddit_extractor.py) ---
import os
from pathlib import Path

def load_dotenv_from(path: str = ".env"):
    """Minimal .env loader: reads KEY=VALUE lines and sets env vars if not already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # Only set if not already present in env (so CLI/env vars can override)
        if key and os.getenv(key) is None:
            os.environ[key] = val

# Load .env (if present) before argparse so flags/env fallback work
load_dotenv_from()  # looks for ./ .env by default
# -------------------------------------------------------------


#!/usr/bin/env python3
"""
Reddit Alexandria (VA) Incident Harvester — v3 (OAuth + progress)
- Uses OAuth client-credentials when provided (much fewer 429s).
- Falls back to public endpoints if creds are missing.
- Scans /new for each subreddit and filters locally by keywords.
"""
import argparse, csv, os, sys, time, re, random
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import requests

DEFAULT_SUBS = ["AlexandriaVA","nova","ArlingtonVA","washingtondc"]
PLACE_ANCHORS = ["Alexandria","Del Ray","Old Town","Potomac Yard","Eisenhower","King St","Duke St",
                 "Van Dorn","Seminary","Beauregard","Landmark","Cameron","Slaters","Huntington",
                 "Fairfax County","Arlandria","Rosemont","North Ridge","West End"]
KEYWORDS = ([
    "fire","smoke","explosion","gas leak","evacuation","hazmat","shelter in place",
    "shooting","shots fired","stabbing","assault",
    "accident","crash","pileup","hit and run",
    "flood","flash flood","water main","sinkhole",
    "power outage","downed lines","transformer",
    "road closed","bridge closed","police activity","sirens","helicopter","medevac",
] + PLACE_ANCHORS)
HIGH_PRIORITY_PATTERNS = [
    r"\b(shooting|shots\s+fired|stabbing|explosion|hazmat|shelter\s+in\s+place)\b",
    r"\b(major\s+accident|multi-vehicle\s+crash|pileup)\b",
    r"\b(active\s+police\s+activity|police\s+blocked|crime\s+scene)\b",
    r"\b(large\s+fire|structure\s+fire|apartment\s+fire)\b",
]

# ---------- OAuth ----------
class RedditAuth:
    def __init__(self, client_id: Optional[str], client_secret: Optional[str], user_agent: str):
        self.client_id, self.client_secret, self.user_agent = client_id, client_secret, user_agent
        self._token, self._exp = None, 0.0

    @property
    def has_creds(self): return bool(self.client_id and self.client_secret)
    def base_url(self): return "https://oauth.reddit.com" if self.has_creds else "https://www.reddit.com"

    def headers(self):
        h = {"User-Agent": self.user_agent}
        if self.has_creds:
            tok = self._ensure_token()
            if tok: h["Authorization"] = f"Bearer {tok}"
        return h

    def _ensure_token(self):
        now = time.time()
        if self._token and now < self._exp - 30: return self._token
        try:
            auth = requests.auth.HTTPBasicAuth(self.client_id, self.client_secret)
            r = requests.post("https://www.reddit.com/api/v1/access_token",
                              auth=auth, data={"grant_type":"client_credentials"},
                              headers={"User-Agent": self.user_agent}, timeout=12)
            r.raise_for_status()
            j = r.json(); self._token = j.get("access_token"); self._exp = now + int(j.get("expires_in",3600))
            return self._token
        except Exception as e:
            print(f"[WARN] OAuth token fetch failed; using unauthenticated mode: {e}", file=sys.stderr)
            self._token, self._exp = None, 0.0
            return None

# ---------- helpers ----------
def now_utc(): return datetime.now(timezone.utc)
def to_iso(ts: float): return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
def contains_keywords(text: str, kws: List[str]): 
    low = text.lower(); return sorted({k for k in kws if k.lower() in low})
def high_priority(text: str):
    low = text.lower(); return any(re.search(p, low) for p in HIGH_PRIORITY_PATTERNS)
def jitter(ms: int) -> float: return random.uniform(ms*0.7, ms*1.3)/1000.0

def get_json(url: str, params: dict, auth: RedditAuth, timeout: int, sleep_ms: int, verbose: bool, label: str) -> dict:
    tries = 0
    while True:
        try:
            if verbose: print(f"[GET] {label} {url} params={params}", flush=True)
            r = requests.get(url, headers=auth.headers(), params=params, timeout=timeout)
            if r.status_code == 429:
                tries += 1; wait = min(30, 2**tries)
                if verbose: print(f"[RATE] 429 on {label}. backoff {wait}s", flush=True)
                time.sleep(wait); continue
            r.raise_for_status()
            if sleep_ms: time.sleep(jitter(sleep_ms))
            return r.json()
        except Exception as e:
            tries += 1
            if tries > 3:
                if verbose: print(f"[WARN] GET failed {label}: {e}", flush=True)
                return {}
            wait = min(20, 2**tries)
            if verbose: print(f"[RETRY] {label}: {e} -> {wait}s", flush=True)
            time.sleep(wait)

def list_new_posts(sub: str, since_utc: float, max_items: int, auth: RedditAuth, timeout: int, sleep_ms: int, verbose: bool):
    url = f"{auth.base_url()}/r/{sub}/new.json"; params = {"limit":"100"}
    out, after, page = [], None, 0
    while len(out) < max_items:
        if after: params["after"] = after
        page += 1
        data = get_json(url, params, auth, timeout, sleep_ms, verbose, f"{sub} page={page}")
        children = data.get("data", {}).get("children", [])
        if verbose: print(f"[{sub}] page {page} got {len(children)}", flush=True)
        if not children: break
        for ch in children:
            p = ch.get("data", {}); created = float(p.get("created_utc", 0) or 0)
            if created < since_utc:
                if verbose: print(f"[{sub}] reached older than window; stop", flush=True)
                return out
            out.append(p)
            if len(out) >= max_items: return out
        after = data.get("data", {}).get("after")
        if not after: break
    return out

def list_comments(sub: str, post_id: str, limit: int, auth: RedditAuth, timeout: int, sleep_ms: int, verbose: bool):
    url = f"{auth.base_url()}/r/{sub}/comments/{post_id}.json"
    data = get_json(url, {"limit":str(limit),"depth":"1","sort":"new"}, auth, timeout, sleep_ms, verbose, f"comments {sub}/{post_id}")
    if not isinstance(data, list) or len(data) < 2: return []
    out = []
    try:
        for it in data[1].get("data", {}).get("children", []):
            if it.get("kind") != "t1": continue
            body = it.get("data", {}).get("body", "") or ""
            if body: out.append(body)
            if len(out) >= limit: break
    except Exception: pass
    return out

def score_post(title: str, body: str, score: int, num_comments: int) -> float:
    text = f"{title}\n{body}"
    s = min(10, len(contains_keywords(text, KEYWORDS))) * 1.5
    s += (score or 0) * 0.2 + (num_comments or 0) * 0.1
    if high_priority(text): s += 10
    if re.search(r"\balexandria\b", text, re.I): s += 3
    return round(s, 2)

def harvest(subs: List[str], hours: int, max_per_sub: int, include_comments: bool,
            request_timeout: int, comments_limit: int, comments_timeout: int,
            sleep_ms: int, verbose: bool, auth: RedditAuth) -> List[Dict[str, Any]]:
    since_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    rows, scanned, matched = [], 0, 0
    print(f"[START] subs={subs} hours={hours} max_per_sub={max_per_sub} include_comments={include_comments}", flush=True)
    for sub in subs:
        posts = list_new_posts(sub, since_utc, max_per_sub, auth, request_timeout, sleep_ms, verbose)
        if verbose: print(f"[{sub}] fetched {len(posts)} posts", flush=True)
        for i, p in enumerate(posts, 1):
            title = p.get("title","") or ""; body = p.get("selftext","") or ""
            if include_comments:
                cm = list_comments(sub, p.get("id",""), comments_limit, auth, comments_timeout, sleep_ms, verbose)
                body = (body + "\n" + "\n".join(cm)).strip()
            scanned += 1
            if contains_keywords(f"{title}\n{body}", KEYWORDS):
                matched += 1
                rows.append({
                    "id": p.get("id"), "created_utc": p.get("created_utc"),
                    "created_iso": to_iso(float(p.get("created_utc",0))) if p.get("created_utc") else "",
                    "subreddit": p.get("subreddit"), "author": p.get("author"),
                    "title": title, "selftext": p.get("selftext","") or "",
                    "url": p.get("url"), "permalink": "https://reddit.com"+p.get("permalink",""),
                    "score": p.get("score") or 0, "num_comments": p.get("num_comments") or 0,
                    "comments_scanned": 0, "matched_keywords": ";".join(contains_keywords(f"{title}\n{body}", KEYWORDS)),
                    "high_priority": high_priority(f"{title}\n{body}"),
                    "eas_score": score_post(title, body, p.get("score") or 0, p.get("num_comments") or 0)
                })
            if verbose and i % 25 == 0:
                print(f"[{sub}] {i}/{len(posts)} processed | scanned={scanned} matched={matched}", flush=True)
        print(f"[{sub}] done. matched_here={sum(1 for r in rows if r['subreddit']==sub)} | total_matched={matched}", flush=True)
    rows.sort(key=lambda r: (r["eas_score"], r.get("created_utc") or 0), reverse=True)
    print(f"[END] scanned={scanned} matched={matched}", flush=True)
    return rows

def write_csv(rows: List[Dict[str, Any]], out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id","created_utc","created_iso","subreddit","author","title","selftext","url","permalink",
              "score","num_comments","comments_scanned","matched_keywords","high_priority","eas_score"]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in rows: w.writerow(r)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subs", nargs="*", default=DEFAULT_SUBS)
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--max_per_sub", type=int, default=300)
    ap.add_argument("--include_comments", action="store_true")
    ap.add_argument("--request_timeout", type=int, default=12)
    ap.add_argument("--comments_limit", type=int, default=20)
    ap.add_argument("--comments_timeout", type=int, default=12)
    ap.add_argument("--sleep_ms", type=int, default=600)
    ap.add_argument("--verbose", action="store_true")
    # NEW: OAuth flags (fallback to env if omitted)
    ap.add_argument("--client_id", default=os.getenv("REDDIT_CLIENT_ID"))
    ap.add_argument("--client_secret", default=os.getenv("REDDIT_CLIENT_SECRET"))
    ap.add_argument("--user_agent", default=os.getenv("REDDIT_USER_AGENT","alexandria-eas/1.3"))
    ap.add_argument("--out", default="data/alx_reddit.csv")
    args = ap.parse_args()

    auth = RedditAuth(args.client_id, args.client_secret, args.user_agent)
    rows = harvest(args.subs, args.hours, args.max_per_sub, args.include_comments,
                   args.request_timeout, args.comments_limit, args.comments_timeout,
                   args.sleep_ms, args.verbose, auth)
    write_csv(rows, Path(args.out))
    print(f"Wrote {len(rows)} rows -> {args.out}", flush=True)

if __name__ == "__main__":
    main()
