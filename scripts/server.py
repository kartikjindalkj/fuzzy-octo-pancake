"""The dashboard app.  python scripts/server.py   ->  http://127.0.0.1:8765

Serves dashboard/ plus a small local API so the page can do everything itself:
pick folders, peek at a folder, run the whole sort (scan, duplicates, Claude labels,
build folders) with live progress, edit folders, browse photos at full size, move a
photo to another folder, and open folders in Finder.
Listens on 127.0.0.1 only: nothing is reachable from other computers.
"""
import glob, hashlib, io, json, mimetypes, os, random, re, shutil, subprocess, sys, threading, time, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from common import CATEGORIES, DATA, IMG, JEV_OUT, LABELS, ROOT, VID, WORK, load, save

PORT = int(os.environ.get("PORT", 8765))
DASH = os.path.join(ROOT, "dashboard")
PEEK = os.path.join(DASH, "peek")
OVERRIDES = os.path.join(WORK, "overrides.json")
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable
job = {"step": None, "running": False, "log": [], "code": None, "phase": None, "phases": [], "done": 0, "total": 0,
       "started": None, "phase_started": None, "timings": {}}


def has_jev_key():
    if os.environ.get("JEV_KEY") or os.environ.get("TYPESAFE_API_KEY"):
        return True
    env = os.path.join(ROOT, ".env")
    return os.path.exists(env) and bool(re.search(r"^(JEV_KEY|TYPESAFE_API_KEY)=\S", open(env).read(), re.M))


def state():
    inv = load(os.path.join(DATA, "inventory.json"), [])
    src = load(os.path.join(DATA, "source.json"), {})
    claude_files = glob.glob(os.path.join(LABELS, "*.json"))
    return {
        "people_ready": os.path.exists(os.path.join(people_root(src), "faces.json")),
        "categories": load(CATEGORIES, []),
        "source": src.get("source"), "limit": src.get("limit"), "sorted": src.get("sorted"),
        "files": len(inv), "bytes": sum(r["bytes"] for r in inv),
        "steps": {
            "scan": bool(inv),
            "dupes": os.path.exists(os.path.join(DATA, "dupes.json")),
            "label": bool(claude_files) or os.path.exists(JEV_OUT),
            "jev_labels": os.path.exists(JEV_OUT),
            "apply": bool(src.get("sorted")) and os.path.exists(os.path.join(DATA, "labels.json")),
        },
        "jev_key": has_jev_key(),
        "claude_cli": bool(shutil.which("claude")),
        "overrides": len(load(OVERRIDES, {})),
        "job": {k: job[k] for k in ("step", "running", "code", "phase", "phases", "done", "total", "started", "phase_started", "timings")}
               | {"log": job["log"][-40:], "now": time.time()},
        "platform": sys.platform,
    }


def live_labels():
    """Labels written so far (Claude batches + confident Jev answers), for the live animation."""
    out = {}
    for f in sorted(glob.glob(os.path.join(LABELS, "*.json"))):
        out.update({str(k): v for k, v in load(f, {}).items()})
    for k, v in load(JEV_OUT, {}).items():
        if "key" in v and (v.get("confidence", 0) >= 0.7 or k not in out):
            out[k] = v["key"]
    return out


def clear_library():
    """A new folder (or sample size) means the old labels no longer match."""
    for f in (os.path.join(DATA, "labels.json"), os.path.join(DATA, "dupes.json"), JEV_OUT, OVERRIDES):
        if os.path.exists(f):
            os.remove(f)
    for d in (LABELS, os.path.join(WORK, "sheets")):
        shutil.rmtree(d, ignore_errors=True)


def categories_fingerprint():
    return hashlib.sha1(open(CATEGORIES, "rb").read()).hexdigest()[:12]


def stream(cmd):
    """Run one script, keep its output for the log, and read PROGRESS lines."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
    for line in p.stdout:
        line = line.rstrip()
        m = re.match(r"PROGRESS (\d+) (\d+)", line)
        if m:
            job["done"], job["total"] = int(m.group(1)), int(m.group(2))
        else:
            job["log"].append(line)
    return p.wait()


def start_job(step, phases, body):
    if job["running"]:
        raise RuntimeError("Something is already running. Wait for it to finish.")
    now = time.time()
    job.update(step=step, running=True, log=[f"$ {step}"], code=None, phase=None, phases=phases, done=0, total=0,
               started=now, phase_started=now, timings={})
    threading.Thread(target=body, daemon=True).start()


def run_phase(name, cmd):
    job.update(phase=name, done=0, total=0, phase_started=time.time())
    code = stream([PY, "-u"] + cmd)
    job["timings"][name] = round(time.time() - job["phase_started"], 1)
    return code


def sort_all(args):
    source = (args.get("source") or "").strip().rstrip("/")
    dest = (args.get("dest") or "").strip()
    limit = int(args.get("limit") or 0)
    labeler = args.get("labeler") or "claude"
    if not source or not os.path.isdir(os.path.expanduser(source)):
        raise ValueError("Choose the folder with your photos first.")
    if labeler == "claude" and not shutil.which("claude"):
        raise ValueError("Claude Code is not installed, so it can't label the photos. Install it, or use Jev under Advanced.")
    if labeler == "jev" and not has_jev_key():
        raise ValueError("Add JEV_KEY=your-key to a .env file in this folder to use Jev.")
    src = load(os.path.join(DATA, "source.json"), {})
    have = os.path.exists(os.path.join(DATA, "inventory.json"))
    need_scan = not have or src.get("source") != os.path.expanduser(source) or int(src.get("limit") or 0) != limit
    phases = (["scan"] if need_scan else []) + ["dupes", "label", "apply"]

    def body():
        def fail(code):
            job.update(running=False, code=code)
        if need_scan:
            clear_library()
            if run_phase("scan", [os.path.join(SCRIPTS, "scan.py"), source] + (["--limit", str(limit)] if limit else [])):
                return fail(1)
        if run_phase("dupes", [os.path.join(SCRIPTS, "dupes.py")]):
            return fail(1)
        marker = os.path.join(LABELS, ".categories")
        if load(marker) != categories_fingerprint():  # folders changed since the last labels
            shutil.rmtree(LABELS, ignore_errors=True)
            if os.path.exists(JEV_OUT):
                os.remove(JEV_OUT)
        os.makedirs(LABELS, exist_ok=True)
        save(marker, categories_fingerprint())
        script = "label_jev.py" if labeler == "jev" else "label_claude.py"
        if run_phase("label", [os.path.join(SCRIPTS, script)]):
            total = len(load(os.path.join(DATA, "inventory.json"), []))
            if not total or len(live_labels()) < 0.95 * total:
                return fail(1)
            job["log"].append(f"A few files could not be labelled; they go to the catch-all folders.")
        if run_phase("apply", [os.path.join(SCRIPTS, "apply.py"), dest]):
            return fail(1)
        job.update(running=False, code=0, phase="done")
    start_job("sort", phases, body)


def run_single(step, args):
    cmds = {
        "scan": ["scan.py", args.get("source", "")] + (["--limit", str(args["limit"])] if args.get("limit") else []),
        "dupes": ["dupes.py"], "estimate": ["label_jev.py", "--estimate"], "jev": ["label_jev.py"],
        "claude": ["label_claude.py"], "apply": ["apply.py", args.get("dest", "")],
    }
    if step not in cmds:
        raise ValueError("unknown step")

    def body():
        if step == "scan":
            clear_library()
        code = run_phase(step, [os.path.join(SCRIPTS, cmds[step][0])] + cmds[step][1:])
        job.update(running=False, code=code, phase="done" if code == 0 else step)
    start_job(step, [step], body)


def run_faces(args):
    """Group the already-chosen photos folder by face into '<folder> (people)'. Fully local."""
    src = load(os.path.join(DATA, "source.json"), {})
    source = (args.get("source") or src.get("source") or "").strip().rstrip("/")
    if not source or not os.path.isdir(os.path.expanduser(source)):
        raise ValueError("Choose or sort a folder first, then the photos can be grouped by face.")
    dest = (args.get("dest") or (source + " (people)")).strip()
    src["people"] = os.path.abspath(os.path.expanduser(dest))
    save(os.path.join(DATA, "source.json"), src)

    def body():
        code = run_phase("faces", [os.path.join(SCRIPTS, "faces.py"), source, dest])
        job.update(running=False, code=code, phase="done" if code == 0 else "faces")
    start_job("faces", ["faces"], body)


PTHUMBS = os.path.join(WORK, "pthumbs")


def people_root(src=None):
    """The _People folder, wherever the face grouping put it."""
    src = load(os.path.join(DATA, "source.json"), {}) if src is None else src
    people = src.get("people") or ((src.get("source") or "").rstrip("/") + " (people)" if src.get("source") else "")
    return os.path.join(people, "_People") if people else ""


def manifest_path():
    root = people_root()
    return os.path.join(root, "faces.json") if root else ""


def safe_people_path(p):
    """Resolve a path only if it sits inside the photos folder or the (people) output. Blocks path tricks."""
    src = load(os.path.join(DATA, "source.json"), {})
    bases = [os.path.realpath(os.path.dirname(people_root(src)))] if people_root(src) else []
    if src.get("source"):
        bases.append(os.path.realpath(os.path.expanduser(src["source"])))
    rp = os.path.realpath(os.path.expanduser(p or ""))
    return rp if any(rp == b or rp.startswith(b + os.sep) for b in bases if b) else None


def make_thumb(path):
    rp = safe_people_path(path)
    if not rp or not os.path.exists(rp):
        return None
    os.makedirs(PTHUMBS, exist_ok=True)
    key = hashlib.sha1(f"{rp}:{os.path.getmtime(rp)}".encode()).hexdigest()[:16]
    out = os.path.join(PTHUMBS, key + ".jpg")
    if not os.path.exists(out):
        from PIL import Image, ImageOps
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        im = ImageOps.exif_transpose(Image.open(rp)).convert("RGB")
        im.thumbnail((320, 320))
        im.save(out, "JPEG", quality=80)
    return out


def make_face_thumb(path, box):
    """Crop a photo to one face (box = x1,y1,x2,y2 as 0..1 fractions) for a group's cover thumbnail."""
    rp = safe_people_path(path)
    if not rp or not os.path.exists(rp):
        return None
    os.makedirs(PTHUMBS, exist_ok=True)
    key = hashlib.sha1(f"{rp}:{os.path.getmtime(rp)}:{box}".encode()).hexdigest()[:16]
    out = os.path.join(PTHUMBS, "f" + key + ".jpg")
    if not os.path.exists(out):
        from PIL import Image, ImageOps
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        im = ImageOps.exif_transpose(Image.open(rp)).convert("RGB")
        W, H = im.size
        x1, y1, x2, y2 = box[0] * W, box[1] * H, box[2] * W, box[3] * H
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        half = max(x2 - x1, y2 - y1) * 0.8  # pad out from the face box to a comfortable head-and-shoulders crop
        crop = im.crop((int(max(0, cx - half)), int(max(0, cy - half)),
                        int(min(W, cx + half)), int(min(H, cy + half))))
        crop.thumbnail((320, 320))
        crop.save(out, "JPEG", quality=82)
    return out


def people_rename(cur_folder, new_name):
    mf = load(manifest_path(), None)
    if not mf:
        raise ValueError("Group by faces first.")
    root = mf["root"]
    person = next((pp for pp in mf["people"] if pp["folder"] == cur_folder), None)
    if not person:
        raise ValueError("That person is no longer here.")
    base = (new_name or "").replace("/", "-").strip() or person["key"]
    taken = {pp["folder"] for pp in mf["people"] if pp is not person}
    nf, i = base, 2
    while nf != cur_folder and (nf in taken or os.path.isdir(os.path.join(root, nf))):
        nf = f"{base} ({i})"
        i += 1
    if nf != cur_folder and os.path.isdir(os.path.join(root, cur_folder)):
        os.rename(os.path.join(root, cur_folder), os.path.join(root, nf))
    old_dir, new_dir = os.path.join(root, cur_folder) + os.sep, os.path.join(root, nf) + os.sep
    for ph in person["photos"]:
        ph["clone"] = ph["clone"].replace(old_dir, new_dir, 1)
    person["folder"], person["name"] = nf, (new_name or "").strip() or person["key"]
    save(manifest_path(), mf)
    names = load(os.path.join(WORK, "face_names.json"), {})
    names[person["key"]] = person["name"]
    save(os.path.join(WORK, "face_names.json"), names)
    return mf


def review_assign(clone, folder_name):
    mf = load(manifest_path(), None)
    if not mf:
        raise ValueError("Group by faces first.")
    root = mf["root"]
    item = next((r for r in mf["review"] if r["clone"] == clone), None)
    person = next((pp for pp in mf["people"] if pp["folder"] == folder_name), None)
    if not item:
        raise ValueError("That photo was already handled.")
    if not person:
        raise ValueError("Pick a person.")
    rp = safe_people_path(clone)
    dst_dir = os.path.join(root, person["folder"])
    os.makedirs(dst_dir, exist_ok=True)
    target = os.path.join(dst_dir, os.path.basename(clone))
    if os.path.exists(target):
        b, e = os.path.splitext(os.path.basename(clone))
        target = os.path.join(dst_dir, f"{b}__{abs(hash(clone)) % 10000}{e}")
    if rp and os.path.exists(rp):
        os.rename(rp, target)
    mf["review"] = [r for r in mf["review"] if r["clone"] != clone]
    person["photos"].append({"path": item["path"], "clone": target})
    person["count"] = len(person["photos"])
    save(manifest_path(), mf)
    return mf


def review_dismiss(clone):
    mf = load(manifest_path(), None)
    if not mf:
        raise ValueError("Group by faces first.")
    rp = safe_people_path(clone)
    root = os.path.realpath(mf["root"])
    if rp and rp.startswith(root + os.sep) and os.path.exists(rp):  # only ever delete a copy inside _People
        os.remove(rp)
    mf["review"] = [r for r in mf["review"] if r["clone"] != clone]
    save(manifest_path(), mf)
    return mf


def people_merge(src_folder, dst_folder):
    """Fold one person's photos into another (the face model split the same person in two). Keeps dst's name."""
    mf = load(manifest_path(), None)
    if not mf:
        raise ValueError("Group by faces first.")
    root = mf["root"]
    src = next((p for p in mf["people"] if p["folder"] == src_folder), None)
    dst = next((p for p in mf["people"] if p["folder"] == dst_folder), None)
    if not src or not dst or src is dst:
        raise ValueError("Pick two different people to merge.")
    dst_dir = os.path.join(root, dst["folder"])
    os.makedirs(dst_dir, exist_ok=True)
    have = {ph["path"] for ph in dst["photos"]}
    for ph in src["photos"]:
        if ph["path"] in have:   # same group photo already filed under dst; its src copy drops with the folder
            continue
        name = os.path.basename(ph["clone"])
        target = os.path.join(dst_dir, name)
        if os.path.exists(target):
            b, e = os.path.splitext(name)
            target = os.path.join(dst_dir, f"{b}__{abs(hash(ph['clone'])) % 10000}{e}")
        rp = safe_people_path(ph["clone"])
        if rp and os.path.exists(rp):
            os.rename(rp, target)
        dst["photos"].append({"path": ph["path"], "clone": target})
        have.add(ph["path"])
    dst["count"] = len(dst["photos"])
    sp = safe_people_path(os.path.join(root, src["folder"]))
    if sp and os.path.isdir(sp):
        shutil.rmtree(sp, ignore_errors=True)
    mf["people"] = [p for p in mf["people"] if p is not src]
    save(manifest_path(), mf)
    return mf


def people_delete(folder):
    """Remove one face group: delete its copied folder inside _People only. Originals are never touched."""
    mf = load(manifest_path(), None)
    if not mf:
        raise ValueError("Group by faces first.")
    person = next((p for p in mf["people"] if p["folder"] == folder), None)
    if not person:
        raise ValueError("That group is no longer here.")
    sp = safe_people_path(os.path.join(mf["root"], person["folder"]))
    if sp and os.path.realpath(sp).startswith(os.path.realpath(mf["root"]) + os.sep) and os.path.isdir(sp):
        shutil.rmtree(sp, ignore_errors=True)  # clones only; the originals live elsewhere and stay put
    mf["people"] = [p for p in mf["people"] if p is not person]
    save(manifest_path(), mf)
    return mf


def peek(source):
    """Count what is in a folder and make a few tiny previews, right after it is picked."""
    source = os.path.expanduser(source.strip().rstrip("/"))
    if not os.path.isdir(source):
        raise ValueError("That folder does not exist.")
    photos, videos, size, pics = 0, 0, 0, []
    for dp, dirs, fs in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith(" (sorted)")]
        for f in fs:
            ext = os.path.splitext(f)[1].lower()
            if f.startswith("._"):
                continue
            if ext in IMG:
                photos += 1
                if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
                    pics.append(os.path.join(dp, f))
            elif ext in VID:
                videos += 1
            else:
                continue
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    shutil.rmtree(PEEK, ignore_errors=True)
    os.makedirs(PEEK)
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    out, stamp = [], int(time.time())
    for i, p in enumerate(random.Random(3).sample(pics, min(60, len(pics)))):
        try:
            with Image.open(p) as im:
                im.draft("RGB", (160, 160))
                im = ImageOps.exif_transpose(im).convert("RGB")
                s = min(im.size)
                im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2)).resize((64, 64))
                im.save(os.path.join(PEEK, f"{i}.jpg"), quality=70)
                out.append(f"peek/{i}.jpg?{stamp}")
        except Exception:
            pass
    return {"source": source, "photos": photos, "videos": videos, "bytes": size, "previews": out,
            "suggested_dest": source + " (sorted)"}


def open_path(path, reveal=False):
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path] if reveal else ["open", path])
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", "/select," + path] if reveal else ["explorer", path])
    else:
        subprocess.run(["xdg-open", os.path.dirname(path) if reveal else path])


def pick_folder(prompt):
    """Native macOS folder picker, shown in front of the browser."""
    if sys.platform != "darwin":
        return None
    prompt = prompt.replace('"', "'")[:120]
    script = ('tell application (path to frontmost application as text)\n'
              f'  POSIX path of (choose folder with prompt "{prompt}")\n'
              'end tell')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip().rstrip("/") or None


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DASH, **k)

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def end_headers(self):
        if self.path.startswith(("/data/", "/peek/")) or self.path.endswith((".js", ".css", ".html")) or self.path == "/":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/state":
            return self.send_json(state())
        if u.path == "/api/labels":
            return self.send_json(live_labels())
        m = re.match(r"^/api/file/(\d+)$", u.path)
        if m:
            return self.send_original(int(m.group(1)))
        if u.path in ("/api/pthumb", "/api/pfile"):
            p = (urllib.parse.parse_qs(u.query).get("p") or [""])[0]
            return self.send_people_image(p, thumb=u.path.endswith("thumb"))
        if u.path == "/api/pface":
            q = urllib.parse.parse_qs(u.query)
            p = (q.get("p") or [""])[0]
            try:
                box = [float(v) for v in (q.get("box") or [""])[0].split(",")][:4]
            except ValueError:
                box = []
            out = make_face_thumb(p, box) if len(box) == 4 else None
            if not out:
                return self.send_error(404)
            return self.send_bytes(open(out, "rb").read(), "image/jpeg", "max-age=300")
        return super().do_GET()

    def send_bytes(self, b, ctype, cache="no-store"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_people_image(self, p, thumb):
        if thumb:
            out = make_thumb(p)
            if not out:
                return self.send_error(404)
            return self.send_bytes(open(out, "rb").read(), "image/jpeg", "max-age=300")
        rp = safe_people_path(p)
        if not rp or not os.path.exists(rp):
            return self.send_error(404)
        if os.path.splitext(rp)[1].lower() in (".heic", ".heif", ".tif", ".tiff", ".bmp"):  # browsers can't show these
            from PIL import Image, ImageOps
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            except ImportError:
                pass
            im = ImageOps.exif_transpose(Image.open(rp)).convert("RGB")
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            return self.send_bytes(buf.getvalue(), "image/jpeg", "max-age=300")
        return self.send_bytes(open(rp, "rb").read(), mimetypes.guess_type(rp)[0] or "application/octet-stream", "max-age=300")

    def send_original(self, i):
        inv = load(os.path.join(DATA, "inventory.json"), [])
        rec = next((r for r in inv if r["id"] == i), None)
        if not rec or not os.path.exists(rec["path"]):
            return self.send_error(404)
        path = rec["path"]
        if rec["ext"] in (".heic", ".heif", ".tif", ".tiff", ".bmp"):  # browsers can't show these: convert
            from PIL import Image, ImageOps
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            except ImportError:
                pass
            im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            b = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if rec["ext"] == ".mov":
            ctype = "video/mp4"  # most iPhone .mov files play fine as mp4
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                start = int(m.group(1) or 0)
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send_json({"error": "forbidden"}, 403)  # only this page may call the API
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/api/sort":
                sort_all(body)
            elif p == "/api/run":
                run_single(body.get("step"), body)
            elif p == "/api/faces":
                run_faces(body)
            elif p == "/api/people":
                return self.send_json(load(manifest_path(), None) or {"people": [], "other": {"photos": []}, "review": []})
            elif p == "/api/people/rename":
                return self.send_json(people_rename(body["folder"], body.get("name", "")))
            elif p == "/api/review/assign":
                return self.send_json(review_assign(body["clone"], body["folder"]))
            elif p == "/api/review/dismiss":
                return self.send_json(review_dismiss(body["clone"]))
            elif p == "/api/people/merge":
                return self.send_json(people_merge(body["from"], body["into"]))
            elif p == "/api/people/delete":
                return self.send_json(people_delete(body["folder"]))
            elif p == "/api/peek":
                return self.send_json(peek(body.get("source", "")))
            elif p == "/api/categories":
                cats = body["categories"]
                keys = [c["key"] for c in cats]
                if len(keys) != len(set(keys)) or not all(c.get("label") and c.get("kind") in ("photo", "video") for c in cats):
                    raise ValueError("Every folder needs a name.")
                if not any(c["kind"] == "photo" for c in cats) or not any(c["kind"] == "video" for c in cats):
                    raise ValueError("Keep at least one photo folder and one video folder.")
                save(CATEGORIES, cats)
                save(os.path.join(DATA, "categories.json"), cats)
            elif p == "/api/move":
                ov = load(OVERRIDES, {})
                ov[str(body["id"])] = body["key"]
                save(OVERRIDES, ov)
                labels = load(os.path.join(DATA, "labels.json"), {})
                labels[str(body["id"])] = body["key"]
                save(os.path.join(DATA, "labels.json"), labels)
            elif p == "/api/open":
                src = load(os.path.join(DATA, "source.json"), {})
                if body.get("people"):
                    people = src.get("people") or ((src.get("source") or "").rstrip("/") + " (people)")
                    root = os.path.join(people, "_People")
                    open_path(root if os.path.isdir(root) else people)
                elif body.get("id") is not None:
                    rec = next(r for r in load(os.path.join(DATA, "inventory.json")) if r["id"] == int(body["id"]))
                    open_path(rec["path"], reveal=True)
                elif src.get("sorted"):
                    folder = body.get("folder")
                    path = os.path.join(src["sorted"], folder) if folder else src["sorted"]
                    open_path(path if os.path.isdir(path) else src["sorted"])
                else:
                    raise ValueError("Sort first, then the folders can be opened.")
            elif p == "/api/pick":
                return self.send_json({"path": pick_folder(body.get("prompt") or "Choose the folder with your photos")})
            else:
                return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)
        return self.send_json(state())


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    print(f"Photo sorter running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
