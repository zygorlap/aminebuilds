"""
Instagram Public Archiver — Session cookie auth to bypass 429
No password stored. Only accesses public profile data.
"""

import os, json, sys, time, shutil, urllib.request
from pathlib import Path
from datetime import datetime, timezone

import instaloader
from instaloader.exceptions import (
    ProfileNotExistsException,
    InstaloaderException,
    QueryReturnedNotFoundException,
)

# ── Config ────────────────────────────────────────────────────────────────────
TARGET     = os.environ["TARGET"].strip().lstrip("@")
MAX_POSTS  = int(os.environ.get("MAX_POSTS", "0"))
DL_VIDEOS  = os.environ.get("DL_VIDEOS", "true").lower() == "true"
DL_REELS   = os.environ.get("DL_REELS", "true").lower() == "true"
DL_TAGGED  = os.environ.get("DL_TAGGED", "false").lower() == "true"
SESSION_ID = os.environ.get("IG_SESSION_ID", "").strip()  # cookie only, no password

# ── Paths ─────────────────────────────────────────────────────────────────────
ARCHIVE_ROOT = Path("instagram_archive") / TARGET
MEDIA_DIR    = ARCHIVE_ROOT / "media"
ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Instaloader ───────────────────────────────────────────────────────────────
L = instaloader.Instaloader(
    dirname_pattern=str(MEDIA_DIR),
    filename_pattern="{shortcode}",
    download_pictures=True,
    download_videos=DL_VIDEOS,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    post_metadata_txt_pattern="",
    compress_json=False,
    quiet=True,
    sleep=True,
    max_connection_attempts=5,
    request_timeout=60,
)

# ── Auth: inject session cookie (bypasses 429, no password needed) ─────────
if SESSION_ID:
    log("Injecting session cookie...")
    import http.cookiejar, requests
    # Build a requests session with the cookie
    session = requests.Session()
    session.cookies.set("sessionid", SESSION_ID, domain=".instagram.com")
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": "936619743392459",
    })
    L.context._session = session
    log("Session cookie applied.")
else:
    log("WARNING: No IG_SESSION_ID provided. Requests may be rate-limited (429).")
    log("         Add your Instagram sessionid cookie as a GitHub secret.")

# ── Load profile ──────────────────────────────────────────────────────────────
log(f"Loading profile: @{TARGET}")
try:
    profile = instaloader.Profile.from_username(L.context, TARGET)
except ProfileNotExistsException:
    log(f"ERROR: @{TARGET} does not exist.")
    sys.exit(1)
except Exception as e:
    log(f"ERROR: {e}")
    sys.exit(1)

if profile.is_private:
    log("ERROR: This account is private. Only public accounts are supported.")
    sys.exit(1)

log(f"OK: {profile.full_name} | {profile.followers:,} followers | {profile.mediacount} posts")

# ── Profile metadata ──────────────────────────────────────────────────────────
profile_data = {
    "username":       profile.username,
    "full_name":      profile.full_name,
    "biography":      profile.biography,
    "external_url":   str(profile.external_url) if profile.external_url else "",
    "followers":      profile.followers,
    "followees":      profile.followees,
    "post_count":     profile.mediacount,
    "is_verified":    profile.is_verified,
    "is_business":    profile.is_business_account,
    "profile_pic_url": str(profile.profile_pic_url),
    "archived_at":    datetime.now(timezone.utc).isoformat(),
}
with open(ARCHIVE_ROOT / "profile.json", "w", encoding="utf-8") as f:
    json.dump(profile_data, f, indent=2, ensure_ascii=False)

# ── Profile picture ───────────────────────────────────────────────────────────
try:
    pic_path = ARCHIVE_ROOT / "profile_picture.jpg"
    if not pic_path.exists():
        urllib.request.urlretrieve(str(profile.profile_pic_url), pic_path)
        log("Downloaded profile picture.")
except Exception as e:
    log(f"Profile picture error: {e}")

# ── Download posts ─────────────────────────────────────────────────────────────
log(f"Downloading posts (max={'ALL' if MAX_POSTS == 0 else MAX_POSTS}, videos={DL_VIDEOS})...")

posts_data = []
downloaded = skipped = errors = 0
error_log  = []

TYPE_MAP = {
    "GraphImage":    "Photo",
    "GraphSidecar":  "Carousel",
    "GraphVideo":    "Video/Reel",
}

for idx, post in enumerate(profile.get_posts(), 1):
    if MAX_POSTS > 0 and idx > MAX_POSTS:
        break

    ptype = TYPE_MAP.get(post.typename, post.typename)
    log(f"[{idx:>4}] {ptype:12s} {post.shortcode}  {post.date_utc.strftime('%Y-%m-%d')}")

    if post.is_video and not DL_VIDEOS:
        skipped += 1
        continue

    meta = {
        "shortcode":  post.shortcode,
        "permalink":  f"https://www.instagram.com/p/{post.shortcode}/",
        "caption":    post.caption or "",
        "timestamp":  post.date_utc.isoformat(),
        "likes":      post.likes,
        "comments":   post.comments,
        "type":       ptype,
        "is_video":   post.is_video,
        "location":   post.location.name if post.location else "",
        "hashtags":   list(post.caption_hashtags) if post.caption_hashtags else [],
        "mentions":   list(post.caption_mentions) if post.caption_mentions else [],
        "files":      [],
        "status":     "ok",
    }

    try:
        L.download_post(post, target=MEDIA_DIR)
        for ext in ("jpg", "jpeg", "mp4", "webp"):
            for f in sorted(MEDIA_DIR.glob(f"{post.shortcode}*.{ext}")):
                if f.name not in meta["files"]:
                    meta["files"].append(f.name)
        downloaded += 1
    except Exception as e:
        log(f"       ERROR: {e}")
        meta["status"] = str(e)
        errors += 1
        error_log.append({"shortcode": post.shortcode, "reason": str(e)})

    posts_data.append(meta)
    time.sleep(1.5)

# ── Reels ─────────────────────────────────────────────────────────────────────
if DL_REELS and DL_VIDEOS:
    log("Checking reels...")
    seen = {p["shortcode"] for p in posts_data}
    rcount = 0
    try:
        for reel in profile.get_reels():
            if MAX_POSTS > 0 and rcount >= MAX_POSTS:
                break
            if reel.shortcode not in seen:
                log(f"  Reel: {reel.shortcode}")
                try:
                    L.download_post(reel, target=MEDIA_DIR)
                    rcount += 1
                    downloaded += 1
                except Exception as e:
                    log(f"  Reel error: {e}")
    except Exception as e:
        log(f"Reels error: {e}")
    if rcount:
        log(f"Downloaded {rcount} extra reels.")

# ── Tagged posts ──────────────────────────────────────────────────────────────
if DL_TAGGED:
    tagged_dir = ARCHIVE_ROOT / "tagged"
    tagged_dir.mkdir(exist_ok=True)
    tagged_data = []
    log("Downloading tagged posts...")
    try:
        for tp in profile.get_tagged_posts():
            try:
                L.download_post(tp, target=tagged_dir)
                tagged_data.append({
                    "shortcode": tp.shortcode,
                    "permalink": f"https://www.instagram.com/p/{tp.shortcode}/",
                    "timestamp": tp.date_utc.isoformat(),
                    "owner":     tp.owner_username,
                })
            except Exception as e:
                log(f"  Tagged error: {e}")
    except Exception as e:
        log(f"Tagged error: {e}")
    if tagged_data:
        with open(ARCHIVE_ROOT / "tagged_metadata.json", "w", encoding="utf-8") as f:
            json.dump(tagged_data, f, indent=2, ensure_ascii=False)
        log(f"Tagged posts saved: {len(tagged_data)}")

# ── Clean up sidecar files ────────────────────────────────────────────────────
for junk in list(MEDIA_DIR.glob("*.json*")) + list(MEDIA_DIR.glob("*.txt")):
    try: junk.unlink()
    except Exception: pass

# ── Save metadata ──────────────────────────────────────────────────────────────
with open(ARCHIVE_ROOT / "posts_metadata.json", "w", encoding="utf-8") as f:
    json.dump(posts_data, f, indent=2, ensure_ascii=False)
if error_log:
    with open(ARCHIVE_ROOT / "errors.json", "w", encoding="utf-8") as f:
        json.dump(error_log, f, indent=2)

# ── HTML Gallery ───────────────────────────────────────────────────────────────
log("Building gallery.html...")
cards = []
for post in posts_data:
    if not post["files"]:
        continue
    first   = post["files"][0]
    is_vid  = first.endswith(".mp4")
    cap     = (post["caption"] or "")[:140].replace("<","&lt;").replace(">","&gt;")
    date    = post["timestamp"][:10]
    tags    = " ".join(f'<span class="tag">#{t}</span>' for t in post["hashtags"][:5])
    med     = (f'<video src="media/{first}" controls muted playsinline preload="none"></video>'
               if is_vid else f'<img src="media/{first}" loading="lazy" alt="">')
    badge   = (f'<span class="badge">&#x1F5BC; {len(post["files"])}</span>'
               if post["type"] == "Carousel" else
               '<span class="badge">&#9654;</span>' if "Video" in post["type"] else "")
    cards.append(f"""<article class="card">
  <a href="{post['permalink']}" target="_blank" class="thumb">{med}{badge}</a>
  <div class="info">
    <div class="row"><span>&#9829; {post['likes']:,}</span><span>&#128172; {post['comments']:,}</span><time>{date}</time></div>
    <p class="cap">{cap}{'&hellip;' if len(post['caption'] or '') > 140 else ''}</p>
    <div class="tags">{tags}</div>
  </div>
</article>""")

html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>@{TARGET} Archive</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#090909;--s:#161616;--b:#252525;--acc:#e1306c;--t:#f0f0f0;--m:#777;--r:14px}}
body{{background:var(--bg);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
a{{color:inherit;text-decoration:none}}
header{{background:var(--s);border-bottom:1px solid var(--b);padding:32px 24px;text-align:center}}
.avatar{{width:88px;height:88px;border-radius:50%;border:3px solid var(--acc);object-fit:cover;display:block;margin:0 auto 14px}}
.handle{{font-size:1.4rem;font-weight:700}} .handle em{{color:var(--acc);font-style:normal}}
.fname{{color:var(--m);margin:4px 0 14px;font-size:.95rem}}
.sbar{{display:flex;justify-content:center;gap:36px;margin-bottom:12px}}
.sbar div{{text-align:center}} .sbar strong{{display:block;font-size:1.1rem}}
.sbar small{{color:var(--m);font-size:.72rem;text-transform:uppercase;letter-spacing:.04em}}
.bio{{max-width:420px;margin:0 auto;color:#aaa;font-size:.88rem;line-height:1.5}}
.chips{{display:flex;justify-content:center;gap:10px;margin:18px 0 0;flex-wrap:wrap}}
.chip{{background:var(--b);border-radius:99px;padding:4px 14px;font-size:.78rem;color:var(--m)}}
.chip b{{color:var(--t)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px;padding:20px;max-width:1400px;margin:auto}}
.card{{background:var(--s);border:1px solid var(--b);border-radius:var(--r);overflow:hidden;transition:transform .18s,box-shadow .18s}}
.card:hover{{transform:translateY(-4px);box-shadow:0 8px 32px rgba(225,48,108,.15)}}
.thumb{{display:block;position:relative;aspect-ratio:1;overflow:hidden;background:#111}}
.thumb img,.thumb video{{width:100%;height:100%;object-fit:cover;display:block;transition:transform .3s}}
.card:hover .thumb img,.card:hover .thumb video{{transform:scale(1.04)}}
.badge{{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.65);backdrop-filter:blur(4px);border-radius:6px;padding:3px 9px;font-size:.72rem}}
.info{{padding:12px 14px 14px}}
.row{{display:flex;gap:12px;font-size:.78rem;color:var(--m);margin-bottom:6px}} time{{margin-left:auto}}
.cap{{font-size:.82rem;color:#ccc;line-height:1.45;word-break:break-word;max-height:3.6em;overflow:hidden}}
.tags{{margin-top:7px;display:flex;flex-wrap:wrap;gap:5px}}
.tag{{font-size:.7rem;color:var(--acc);background:rgba(225,48,108,.1);border-radius:4px;padding:1px 6px}}
footer{{text-align:center;color:var(--m);padding:32px;font-size:.8rem;border-top:1px solid var(--b)}}
footer a{{color:var(--acc)}}
</style></head><body>
<header>
  <img class="avatar" src="profile_picture.jpg" onerror="this.style.display='none'" alt="">
  <div class="handle">&#64;<em>{TARGET}</em></div>
  <div class="fname">{profile_data.get('full_name','')}</div>
  <div class="sbar">
    <div><strong>{profile_data.get('followers',0):,}</strong><small>Followers</small></div>
    <div><strong>{profile_data.get('followees',0):,}</strong><small>Following</small></div>
    <div><strong>{profile_data.get('post_count',0):,}</strong><small>Posts</small></div>
  </div>
  <p class="bio">{(profile_data.get('biography') or '').replace(chr(10),' ')}</p>
  <div class="chips">
    <div class="chip">Saved <b>{downloaded}</b></div>
    <div class="chip">Errors <b>{errors}</b></div>
    <div class="chip">Date <b>{datetime.now(timezone.utc).strftime('%Y-%m-%d')}</b></div>
    {'<div class="chip">&#10003; Verified</div>' if profile_data.get('is_verified') else ''}
  </div>
</header>
<div class="grid">{''.join(cards)}</div>
<footer>
  Public archive &bull; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} &bull;
  <a href="posts_metadata.json">JSON</a> &bull;
  <a href="https://www.instagram.com/{TARGET}/" target="_blank">Instagram</a>
</footer>
</body></html>"""

with open(ARCHIVE_ROOT / "gallery.html", "w", encoding="utf-8") as f:
    f.write(html)

# ── Summary ───────────────────────────────────────────────────────────────────
log("")
log("=" * 52)
log(f"  @{TARGET}")
log(f"  Posts processed : {len(posts_data)}")
log(f"  Files saved     : {downloaded}")
log(f"  Skipped         : {skipped}")
log(f"  Errors          : {errors}")
log("=" * 52)

gho = os.environ.get("GITHUB_OUTPUT", "")
if gho:
    with open(gho, "a") as f:
        f.write(f"posts_count={len(posts_data)}\n")
        f.write(f"files_saved={downloaded}\n")
        f.write(f"errors={errors}\n")

if downloaded == 0 and len(posts_data) > 0:
    log("FATAL: Posts found but nothing downloaded.")
    sys.exit(1)
