"""
Instagram Public Archiver — No login required
Uses instaloader to download all posts, reels, tagged posts,
metadata, profile info, and generates an HTML gallery.
"""

import os, json, sys, time, shutil
from pathlib import Path
from datetime import datetime, timezone

import instaloader

# ── Config ────────────────────────────────────────────────────────────────────
TARGET       = os.environ["TARGET"].strip().lstrip("@")
MAX_POSTS    = int(os.environ.get("MAX_POSTS", "0"))       # 0 = all
DL_VIDEOS    = os.environ.get("DL_VIDEOS", "true").lower() == "true"
DL_TAGGED    = os.environ.get("DL_TAGGED", "false").lower() == "true"
DL_REELS     = os.environ.get("DL_REELS", "true").lower() == "true"

# ── Paths ─────────────────────────────────────────────────────────────────────
ARCHIVE_ROOT = Path("instagram_archive") / TARGET
MEDIA_DIR    = ARCHIVE_ROOT / "media"
ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ── Instaloader setup ─────────────────────────────────────────────────────────
L = instaloader.Instaloader(
    dirname_pattern=str(MEDIA_DIR),
    filename_pattern="{shortcode}",
    download_pictures=True,
    download_videos=DL_VIDEOS,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,          # we handle metadata ourselves
    post_metadata_txt_pattern="", # no .txt sidecar files
    compress_json=False,
    quiet=True,
    sleep=True,                   # polite delays built-in
    fatal_status_codes=[400, 429],
    max_connection_attempts=3,
    request_timeout=60,
)

# ── Load profile (no login needed for public accounts) ───────────────────────
log(f"Loading public profile: @{TARGET}")
try:
    profile = instaloader.Profile.from_username(L.context, TARGET)
except instaloader.exceptions.ProfileNotExistsException:
    log(f"ERROR: Profile @{TARGET} does not exist.")
    sys.exit(1)
except Exception as e:
    log(f"ERROR loading profile: {e}")
    sys.exit(1)

if profile.is_private:
    log("ERROR: This account is PRIVATE. Only public accounts are supported without login.")
    sys.exit(1)

log(f"Found: {profile.full_name} | {profile.followers:,} followers | {profile.mediacount} posts | verified={profile.is_verified}")

# ── Save profile metadata ─────────────────────────────────────────────────────
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
log("Saved profile.json")

# ── Download profile picture ──────────────────────────────────────────────────
try:
    import urllib.request
    pic_path = ARCHIVE_ROOT / "profile_picture.jpg"
    if not pic_path.exists():
        urllib.request.urlretrieve(str(profile.profile_pic_url), pic_path)
        log("Downloaded profile picture.")
except Exception as e:
    log(f"Could not download profile picture: {e}")

# ── Download posts ─────────────────────────────────────────────────────────────
log(f"Starting download (MAX_POSTS={'ALL' if MAX_POSTS == 0 else MAX_POSTS}, videos={DL_VIDEOS}, reels={DL_REELS})...")

posts_data  = []
downloaded  = 0
skipped     = 0
errors      = 0
error_log   = []

TYPE_NAMES = {
    instaloader.PostType.image:     "Photo",
    instaloader.PostType.sidecar:   "Carousel",
    instaloader.PostType.video:     "Video/Reel",
    instaloader.PostType.igtv:      "IGTV",
}

post_iter = profile.get_posts()

for idx, post in enumerate(post_iter, 1):
    if MAX_POSTS > 0 and idx > MAX_POSTS:
        break

    ptype = TYPE_NAMES.get(post.typename, post.typename)
    log(f"[{idx:>4}] {ptype:10s} | {post.shortcode} | {post.date_utc.strftime('%Y-%m-%d')}")

    # Skip videos/reels if disabled
    if post.is_video and not DL_VIDEOS:
        log(f"       Skipped (video disabled)")
        skipped += 1
        continue

    post_meta = {
        "shortcode":   post.shortcode,
        "permalink":   f"https://www.instagram.com/p/{post.shortcode}/",
        "caption":     post.caption or "",
        "timestamp":   post.date_utc.isoformat(),
        "likes":       post.likes,
        "comments":    post.comments,
        "type":        ptype,
        "is_video":    post.is_video,
        "location":    post.location.name if post.location else "",
        "hashtags":    list(post.caption_hashtags) if post.caption_hashtags else [],
        "mentions":    list(post.caption_mentions) if post.caption_mentions else [],
        "tagged_users": [],
        "files":       [],
        "status":      "ok",
    }

    try:
        # Download — instaloader handles carousels, videos, images automatically
        L.download_post(post, target=MEDIA_DIR)

        # Collect filenames that belong to this post
        post_files = []
        for ext in ["jpg", "jpeg", "mp4", "webp"]:
            matches = sorted(MEDIA_DIR.glob(f"{post.shortcode}*.{ext}"))
            post_files.extend([f.name for f in matches])

        post_meta["files"] = post_files
        downloaded += 1

    except instaloader.exceptions.InstaloaderException as e:
        log(f"       ERROR: {e}")
        post_meta["status"] = f"error: {e}"
        errors += 1
        error_log.append({"shortcode": post.shortcode, "reason": str(e)})
    except Exception as e:
        log(f"       UNEXPECTED ERROR: {e}")
        post_meta["status"] = f"error: {e}"
        errors += 1
        error_log.append({"shortcode": post.shortcode, "reason": str(e)})

    posts_data.append(post_meta)

# ── Download Reels separately (IGTV / Reels feed) ─────────────────────────────
if DL_REELS and DL_VIDEOS:
    log("Checking reels...")
    try:
        reel_count = 0
        for reel in profile.get_reels():
            if MAX_POSTS > 0 and reel_count >= MAX_POSTS:
                break
            # Only download if not already in posts
            already = any(p["shortcode"] == reel.shortcode for p in posts_data)
            if not already:
                log(f"  Reel: {reel.shortcode}")
                try:
                    L.download_post(reel, target=MEDIA_DIR)
                    reel_count += 1
                    downloaded += 1
                except Exception as e:
                    log(f"  Reel error: {e}")
        if reel_count:
            log(f"Downloaded {reel_count} additional reels.")
    except Exception as e:
        log(f"Could not fetch reels: {e}")

# ── Download tagged posts (optional) ──────────────────────────────────────────
tagged_data = []
if DL_TAGGED:
    log("Downloading tagged posts...")
    tagged_dir = ARCHIVE_ROOT / "tagged"
    tagged_dir.mkdir(exist_ok=True)
    try:
        for tidx, tpost in enumerate(profile.get_tagged_posts(), 1):
            log(f"  Tagged [{tidx}]: {tpost.shortcode}")
            try:
                L.download_post(tpost, target=tagged_dir)
                tagged_data.append({
                    "shortcode": tpost.shortcode,
                    "permalink": f"https://www.instagram.com/p/{tpost.shortcode}/",
                    "timestamp": tpost.date_utc.isoformat(),
                    "owner":     tpost.owner_username,
                })
            except Exception as e:
                log(f"  Tagged error: {e}")
    except Exception as e:
        log(f"Could not fetch tagged posts: {e}")
    if tagged_data:
        with open(ARCHIVE_ROOT / "tagged_metadata.json", "w", encoding="utf-8") as f:
            json.dump(tagged_data, f, indent=2, ensure_ascii=False)
        log(f"Saved {len(tagged_data)} tagged posts.")

# ── Clean up instaloader sidecar files (.json.xz etc.) ───────────────────────
for junk in MEDIA_DIR.glob("*.json*"):
    try:
        junk.unlink()
    except Exception:
        pass

# ── Save all metadata ──────────────────────────────────────────────────────────
with open(ARCHIVE_ROOT / "posts_metadata.json", "w", encoding="utf-8") as f:
    json.dump(posts_data, f, indent=2, ensure_ascii=False)
log(f"Saved posts_metadata.json ({len(posts_data)} posts)")

if error_log:
    with open(ARCHIVE_ROOT / "errors.json", "w", encoding="utf-8") as f:
        json.dump(error_log, f, indent=2)
    log(f"Saved errors.json ({len(error_log)} errors)")

# ── Generate HTML Gallery ──────────────────────────────────────────────────────
log("Generating gallery.html...")

cards_html = []
for post in posts_data:
    if not post["files"]:
        continue
    first = post["files"][0]
    is_vid = first.endswith(".mp4")
    caption = (post["caption"] or "")[:140].replace("<","&lt;").replace(">","&gt;")
    date_str = post["timestamp"][:10]
    link = post["permalink"]
    tags = " ".join(f'<span class="tag">#{t}</span>' for t in post["hashtags"][:5])

    if is_vid:
        media_tag = f'<video src="media/{first}" controls muted playsinline preload="none"></video>'
    else:
        media_tag = f'<img src="media/{first}" alt="{post["shortcode"]}" loading="lazy">'

    badge = ""
    if post["type"] == "Carousel":
        badge = f'<span class="badge">&#x1F5BC; {len(post["files"])}</span>'
    elif "Video" in post["type"] or "Reel" in post["type"]:
        badge = '<span class="badge">&#x25B6;</span>'

    cards_html.append(f"""<article class="card">
  <a href="{link}" target="_blank" class="thumb">{media_tag}{badge}</a>
  <div class="info">
    <div class="row"><span>&#9829; {post['likes']:,}</span><span>&#128172; {post['comments']:,}</span><time>{date_str}</time></div>
    <p class="cap">{caption}{'&hellip;' if len(post['caption'] or '') > 140 else ''}</p>
    <div class="tags">{tags}</div>
  </div>
</article>""")

gallery = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@{TARGET} &mdash; Archive</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#090909;--s:#161616;--b:#252525;--acc:#e1306c;--t:#f0f0f0;--m:#777;--r:14px}}
body{{background:var(--bg);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}}
a{{color:inherit;text-decoration:none}}
header{{background:var(--s);border-bottom:1px solid var(--b);padding:32px 24px;text-align:center}}
.avatar{{width:88px;height:88px;border-radius:50%;border:3px solid var(--acc);object-fit:cover;display:block;margin:0 auto 14px}}
.handle{{font-size:1.4rem;font-weight:700}} .handle span{{color:var(--acc)}}
.fname{{color:var(--m);margin:4px 0 14px;font-size:.95rem}}
.sbar{{display:flex;justify-content:center;gap:36px;margin-bottom:12px}}
.sbar div{{text-align:center}} .sbar strong{{display:block;font-size:1.1rem}} .sbar small{{color:var(--m);font-size:.72rem;text-transform:uppercase;letter-spacing:.04em}}
.bio{{max-width:420px;margin:0 auto;color:#aaa;font-size:.88rem;line-height:1.5}}
.chips{{display:flex;justify-content:center;gap:10px;margin:18px 0 0;flex-wrap:wrap}}
.chip{{background:var(--b);border-radius:99px;padding:4px 14px;font-size:.78rem;color:var(--m)}}
.chip span{{color:var(--t);font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px;padding:20px;max-width:1400px;margin:auto}}
.card{{background:var(--s);border:1px solid var(--b);border-radius:var(--r);overflow:hidden;transition:transform .18s,box-shadow .18s}}
.card:hover{{transform:translateY(-4px);box-shadow:0 8px 32px rgba(225,48,108,.15)}}
.thumb{{display:block;position:relative;aspect-ratio:1;overflow:hidden;background:#111}}
.thumb img,.thumb video{{width:100%;height:100%;object-fit:cover;display:block;transition:transform .3s}}
.card:hover .thumb img,.card:hover .thumb video{{transform:scale(1.04)}}
.badge{{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.65);backdrop-filter:blur(4px);border-radius:6px;padding:3px 9px;font-size:.72rem}}
.info{{padding:12px 14px 14px}}
.row{{display:flex;gap:12px;font-size:.78rem;color:var(--m);margin-bottom:6px}} time{{margin-left:auto}}
.cap{{font-size:.82rem;color:#ccc;line-height:1.45;word-break:break-word;white-space:pre-wrap;max-height:3.6em;overflow:hidden}}
.tags{{margin-top:7px;display:flex;flex-wrap:wrap;gap:5px}}
.tag{{font-size:.7rem;color:var(--acc);background:rgba(225,48,108,.1);border-radius:4px;padding:1px 6px}}
footer{{text-align:center;color:var(--m);padding:32px;font-size:.8rem;border-top:1px solid var(--b)}}
footer a{{color:var(--acc)}}
</style>
</head>
<body>
<header>
  <img class="avatar" src="profile_picture.jpg" onerror="this.style.display='none'" alt="@{TARGET}">
  <div class="handle">&#64;<span>{TARGET}</span></div>
  <div class="fname">{profile_data.get('full_name','')}</div>
  <div class="sbar">
    <div><strong>{profile_data.get('followers',0):,}</strong><small>Followers</small></div>
    <div><strong>{profile_data.get('followees',0):,}</strong><small>Following</small></div>
    <div><strong>{profile_data.get('post_count',0):,}</strong><small>Posts</small></div>
  </div>
  <p class="bio">{(profile_data.get('biography') or '').replace(chr(10),' ')}</p>
  <div class="chips">
    <div class="chip">Downloaded <span>{downloaded}</span></div>
    <div class="chip">Errors <span>{errors}</span></div>
    <div class="chip">Archived <span>{datetime.now(timezone.utc).strftime('%Y-%m-%d')}</span></div>
    {'<div class="chip">&#10003; Verified</div>' if profile_data.get('is_verified') else ''}
  </div>
</header>

<div class="grid">
{''.join(cards_html)}
</div>

<footer>
  Archive of public profile &bull; Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} &bull;
  <a href="posts_metadata.json">JSON data</a> &bull;
  <a href="https://www.instagram.com/{TARGET}/" target="_blank">View on Instagram</a>
</footer>
</body>
</html>"""

with open(ARCHIVE_ROOT / "gallery.html", "w", encoding="utf-8") as f:
    f.write(gallery)
log("Saved gallery.html")

# ── Final summary ──────────────────────────────────────────────────────────────
log("")
log("=" * 52)
log(f"  @{TARGET}")
log(f"  Posts processed : {len(posts_data)}")
log(f"  Files saved     : {downloaded}")
log(f"  Skipped         : {skipped}")
log(f"  Errors          : {errors}")
log(f"  Output path     : {ARCHIVE_ROOT}/")
log("=" * 52)

gho = os.environ.get("GITHUB_OUTPUT", "")
if gho:
    with open(gho, "a") as f:
        f.write(f"posts_count={len(posts_data)}\n")
        f.write(f"files_saved={downloaded}\n")
        f.write(f"errors={errors}\n")

if downloaded == 0 and len(posts_data) > 0:
    log("FATAL: Posts were found but nothing downloaded. Check logs above.")
    sys.exit(1)
