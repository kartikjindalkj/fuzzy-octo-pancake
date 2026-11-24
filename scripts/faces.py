"""Optional: group photos by person's face, fully on your computer.

  python scripts/faces.py "/path/to/photos"                 # -> "<photos> (people)"
  python scripts/faces.py "/path/to/photos" "/where/to/put" # choose the output folder
  python scripts/faces.py "/path/to/photos" --limit 200     # try a sample first

Detects faces (InsightFace), turns each into a fingerprint, groups matching faces, and
COPIES each photo into People/Person N/. Doubtful faces (near the line between two people,
low quality, or matching no group) go to People/_Review/ with a reason, so most photos
sort themselves and only the unsure ones need a look. Photos with no face are skipped.

Fully local: your originals are only read, nothing is uploaded, no faces leave your Mac.
Needs: insightface onnxruntime scikit-learn  (installed into .venv by this repo).
Videos are not handled yet; this reads still photos only.
"""
import argparse, csv, json, os, shutil, subprocess, sys, warnings

import numpy as np
from PIL import Image, ImageOps

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(__file__))
from common import IMG, WORK

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# Tunables. Cosine similarity of ArcFace fingerprints: same person ~0.5+, strangers ~0.2.
EPS = float(os.environ.get("FACE_EPS", 0.5))        # cluster distance (1 - cosine); lower = stricter
DET_MIN = float(os.environ.get("FACE_DET_MIN", 0.5))  # ignore weak detections (probably not a face)
DET_GOOD = 0.65        # below this a detected face is "low quality" -> review
SIM_CONF = 0.45        # a face must match its own group at least this well to be confident
MARGIN = 0.07          # own-group match must beat the next-best group by this, else "ambiguous"
MIN_FACE_FRAC = 0.012  # faces smaller than ~1.2% of the image's short side are too small to trust
MIN_CLUSTER = int(os.environ.get("FACE_MIN_CLUSTER", 3))  # groups smaller than this are not a stable person
MIN_PHOTOS = int(os.environ.get("FACE_MIN_PHOTOS", 4))    # a person needs this many photos to earn their own folder
MAX_SIDE = 1280        # downscale big photos in memory before detection (speed; originals untouched)

ap = argparse.ArgumentParser()
ap.add_argument("source")
ap.add_argument("dest", nargs="?", default="")
ap.add_argument("--limit", type=int, default=0, help="only look at a random sample of N photos")
ap.add_argument("--eps", type=float, default=EPS, help="grouping strictness (lower = stricter)")
ap.add_argument("--rescan", action="store_true", help="ignore the cached face scan and look again")
args = ap.parse_args()

src = os.path.expanduser(args.source).rstrip("/")
if not os.path.isdir(src):
    sys.exit(f"Folder not found: {src}")
dest = os.path.expanduser(args.dest) if args.dest else src + " (people)"
if os.path.abspath(dest).startswith(os.path.abspath(src) + os.sep):
    sys.exit("Put the people folder outside the photos folder, or the next scan would count the copies.")

files = sorted(os.path.join(dp, f)
               for dp, dirs, fs in os.walk(src) if not os.path.basename(dp).endswith(" (people)")
               for f in fs
               if os.path.splitext(f)[1].lower() in IMG and not f.startswith("._"))
if not files:
    sys.exit("No photos found in that folder.")
if args.limit and args.limit < len(files):
    import random
    files = sorted(random.Random(0).sample(files, args.limit))


def load_bgr(path):
    """Read an original, fix rotation, downscale, hand InsightFace a cv2-style BGR array."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > MAX_SIDE:
            im.thumbnail((MAX_SIDE, MAX_SIDE))
        return np.asarray(im)[:, :, ::-1]


# Detecting faces is the slow part. Cache the fingerprints so re-grouping (e.g. tuning) is instant.
os.makedirs(WORK, exist_ok=True)
cache_emb = os.path.join(WORK, "faces_cache.npy")
cache_meta = os.path.join(WORK, "faces_cache.json")
faces = None
if not args.rescan and os.path.exists(cache_emb) and os.path.exists(cache_meta):
    meta = json.load(open(cache_meta))
    if meta.get("files") == files and meta.get("v") == 2:  # v2 adds each face's box for cover thumbnails
        arr = np.load(cache_emb)
        faces = [dict(m, emb=arr[i]) for i, m in enumerate(meta["faces"])]
        print(f"Reusing the cached face scan ({len(faces)} faces in {len(files)} photos). Use --rescan to redo it.", flush=True)

if faces is None:
    print(f"Loading the face model (first run downloads it, ~280 MB)...", flush=True)
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))

    print(f"Looking for faces in {len(files)} photos...", flush=True)
    faces = []  # one row per detected face
    for n, path in enumerate(files, 1):
        try:
            img = load_bgr(path)
        except Exception:
            continue
        h, w = img.shape[0], img.shape[1]
        short = min(h, w)
        for f in app.get(img):
            if f.det_score < DET_MIN:
                continue
            x1, y1, x2, y2 = f.bbox
            face_frac = min(x2 - x1, y2 - y1) / short
            box = [max(0.0, min(1.0, float(v))) for v in (x1 / w, y1 / h, x2 / w, y2 / h)]  # fractions: survive later resizing
            faces.append({"path": path, "emb": f.normed_embedding.astype(np.float32),
                          "det": float(f.det_score), "frac": float(face_frac), "box": box})
        if n % 20 == 0 or n == len(files):
            print(f"PROGRESS {n} {len(files)}", flush=True)
    if faces:
        np.save(cache_emb, np.vstack([f["emb"] for f in faces]))
        json.dump({"v": 2, "files": files,
                   "faces": [{"path": f["path"], "det": f["det"], "frac": f["frac"], "box": f["box"]} for f in faces]},
                  open(cache_meta, "w"))

if not faces:
    sys.exit("No faces found. (This groups by face; photos without people stay in the content folders.)")

embs = np.vstack([f["emb"] for f in faces]).astype(np.float32)

from sklearn.cluster import DBSCAN
labels = DBSCAN(eps=args.eps, min_samples=2, metric="cosine").fit_predict(embs)

# Centroid (average fingerprint) of each real group, then rank groups by how common they are.
clusters = {}
for lab, f in zip(labels, faces):
    if lab != -1:
        clusters.setdefault(lab, []).append(f)
stable = {lab: fs for lab, fs in clusters.items() if len(fs) >= MIN_CLUSTER}
centroids = {}
for lab, fs in stable.items():
    c = np.mean([f["emb"] for f in fs], axis=0)
    centroids[lab] = c / (np.linalg.norm(c) + 1e-9)
ranked = sorted(centroids, key=lambda lab: -len(stable[lab]))
pos = {lab: i for i, lab in enumerate(ranked)}
cmat = np.vstack([centroids[lab] for lab in ranked]) if ranked else np.zeros((0, embs.shape[1]), np.float32)


def classify(f, lab):
    """Return (group-label or None, reason, rival-label). A None group sends the photo to _Review."""
    if lab not in centroids:
        return None, "unique face, matches no group", None
    if f["det"] < DET_GOOD or f["frac"] < MIN_FACE_FRAC:
        return None, "face too small or unclear", None
    sims = cmat @ f["emb"]
    own = float(sims[pos[lab]])
    rival = next((i for i in np.argsort(sims)[::-1] if i != pos[lab]), None)
    if own < SIM_CONF:
        return None, "weak match to its group", None
    if rival is not None and own - float(sims[rival]) < MARGIN:
        return None, "ambiguous", ranked[int(rival)]
    return lab, "", None


# Decide every face first, then name only the groups that keep at least one confident photo.
decided = [(f, lab, *classify(f, lab)) for lab, f in zip(labels, faces)]
cover = {}  # group -> (det_score, original_path, box); the clearest face becomes the group's thumbnail
for f, lab, group, reason, rival in decided:
    if group is not None and "box" in f and (group not in cover or f["det"] > cover[group][0]):
        cover[group] = (f["det"], f["path"], f["box"])
confident_photos = {}
for f, lab, group, _, _ in decided:
    if group is not None:
        confident_photos.setdefault(group, set()).add(f["path"])
# Only people who turn up in enough photos get their own folder; one-off faces go to _Other.
promoted = [g for g in sorted(confident_photos, key=lambda g: -len(confident_photos[g]))
            if len(confident_photos[g]) >= MIN_PHOTOS]
width = max(2, len(str(len(promoted))))
default_name = {g: f"Person {i + 1:0{width}d}" for i, g in enumerate(promoted)}  # zero-padded so folders sort by size

# A name typed in the dashboard sticks across re-runs. Kept in work/ because _People is rebuilt each run.
names_file = os.path.join(WORK, "face_names.json")
saved_names = json.load(open(names_file)) if os.path.exists(names_file) else {}


def as_folder(s):
    return (s or "").replace("/", "-").strip() or "Person"


display = {g: saved_names.get(default_name[g], default_name[g]) for g in promoted}
folder, used = {}, set()
for g in promoted:  # two people may share a name; their folders must still be distinct
    base = as_folder(display[g])
    f, i = base, 2
    while f in used:
        f, i = f"{base} ({i})", i + 1
    folder[g], _ = f, used.add(f)
disp_of = lambda g: display.get(g)

photo_people = {}    # path -> set of groups (recurring, named people)
photo_other = set()  # paths whose only people appear in just a few photos
photo_review = {}    # path -> (reason, [candidate names]); used only if the photo has no confident face
for f, lab, group, reason, rival in decided:
    if group in folder:
        photo_people.setdefault(f["path"], set()).add(group)
    elif group is not None:                 # a real match, but this person is in too few photos
        photo_other.add(f["path"])
    else:
        cands = [d for d in (disp_of(lab), disp_of(rival)) if d]
        if reason == "ambiguous":
            reason = "could be " + " or ".join(cands) if cands else "ambiguous match"
        photo_review.setdefault(f["path"], (reason, cands))

other = {p for p in photo_other if p not in photo_people}
review = {p: rc for p, rc in photo_review.items() if p not in photo_people and p not in other}

people_dir = os.path.join(dest, "_People")
shutil.rmtree(people_dir, ignore_errors=True)  # rebuild cleanly so old groups/numbering don't linger
os.makedirs(people_dir, exist_ok=True)


def copy(s, folder_name):
    """Copy one photo into _People/<folder>, return the clone's path (APFS clone on macOS: instant, no space)."""
    out = os.path.join(people_dir, folder_name)
    os.makedirs(out, exist_ok=True)
    target = os.path.join(out, os.path.basename(s))
    if os.path.exists(target):  # same file name coming from another folder
        b, e = os.path.splitext(os.path.basename(s))
        target = os.path.join(out, f"{b}__{abs(hash(s)) % 10000}{e}")
    if not os.path.exists(target):
        if not (sys.platform == "darwin" and subprocess.run(["cp", "-c", "-p", s, target], capture_output=True).returncode == 0):
            shutil.copy2(s, target)
    return target


# faces.json is what the dashboard reads to show, name, and fix the groups without touching Finder.
manifest = {"dest": os.path.abspath(dest), "root": os.path.abspath(people_dir),
            "people": [], "other": {"photos": []}, "review": []}
with open(os.path.join(people_dir, "faces.csv"), "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["file", "person", "note", "original_path"])
    for g in promoted:
        entry = {"key": default_name[g], "name": display[g], "folder": folder[g], "photos": []}
        if g in cover:
            entry["cover"] = {"path": cover[g][1], "box": [round(float(v), 5) for v in cover[g][2]]}
        for path in sorted(p for p, groups in photo_people.items() if g in groups):
            entry["photos"].append({"path": path, "clone": copy(path, folder[g])})
            w.writerow([os.path.basename(path), display[g], "", path])
        entry["count"] = len(entry["photos"])
        manifest["people"].append(entry)
    for path in sorted(other):
        manifest["other"]["photos"].append({"path": path, "clone": copy(path, "_Other")})
        w.writerow([os.path.basename(path), "_Other", "appears in only a few photos", path])
    for path, (reason, cands) in sorted(review.items()):
        manifest["review"].append({"path": path, "clone": copy(path, "_Review"), "reason": reason, "candidates": cands})
        w.writerow([os.path.basename(path), "_Review", reason, path])

json.dump(manifest, open(os.path.join(people_dir, "faces.json"), "w"))

photos_with_faces = len(set(f["path"] for f in faces))
print(f"\nDone. {len(promoted)} recurring people found across {photos_with_faces} photos with faces.")
for e in manifest["people"]:
    print(f"  {e['name']:12} {e['count']} photos")
print(f"  {'_Other':12} {len(other)} photos (people who appear only a few times)")
print(f"  {'_Review':12} {len(review)} photos (unsure - a quick look sorts these)")
print(f"\nPeople folders are in: {people_dir}")
print("Open the dashboard to review, name, and fix them, or rename the folders yourself. Full list: _People/faces.csv")
