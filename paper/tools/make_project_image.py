"""Compose docs/project-image.png (1600x900) from fig1a.pdf and numbers.json.
Run: /Users/amey/.local/wtth/venv/bin/python -I paper/tools/make_project_image.py
"""
import json, shutil, subprocess, tempfile, os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
AN = os.path.join(ROOT, "paper/generated/analysis")
OUT = os.path.join(ROOT, "docs/project-image.png")
W, H = 1600, 900

nums = {e["key"]: e["display"] for e in json.load(open(os.path.join(AN, "numbers.json")))}
p1_nr, p1_f = nums["HaikuPOneNoReceiptPct"], nums["HaikuPOneForgedPct"]
k, n = nums["HaikuPTwoForgedK"], nums["HaikuPTwoForgedN"]
line = (f"A receipt-style line typed into a peer's post cut reporting from {p1_nr}% to {p1_f}%, "
        f"and gave the only pre-registered shifts in which no one told the human ({k}/{n}).")

tmp = tempfile.mkdtemp()
subprocess.run(["pdftoppm", "-png", "-r", "400", "-singlefile",
                os.path.join(AN, "figures/fig1a.pdf"), os.path.join(tmp, "f")], check=True)
fig = Image.open(os.path.join(tmp, "f.png")).convert("RGBA")
bg = Image.new("RGBA", fig.size, "white"); bg.alpha_composite(fig); fig = bg.convert("RGB")

def font(sz, bold=False):
    for p in ["/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf"]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, sz, index=1 if bold else 0)
            except Exception: pass
    return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", sz)

img = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(img)
def center(y, text, f, fill):
    w = d.textlength(text, font=f); d.text(((W - w) / 2, y), text, font=f, fill=fill)
center(18, "Who Tells the Humans?", font(52, True), (20, 20, 20))
center(80, "Phantom Escalation and Channel-Written Receipts in Agent Swarms", font(30), (40, 40, 40))
center(124, "Amey Pawar · Apart Research Swarm Alignment Hackathon 2026", font(20), (90, 90, 90))

# bottom line, wrapped
f = font(25, True); words = line.split(); lines = []; cur = ""
for w_ in words:
    t = (cur + " " + w_).strip()
    if d.textlength(t, font=f) > W - 160: lines.append(cur); cur = w_
    else: cur = t
lines.append(cur)
y0 = H - 25 - 34 * len(lines)
for i, l in enumerate(lines): center(y0 + 34 * i, l, f, (20, 20, 20))

top, bot = 160, y0 - 12
s = min((W - 60) / fig.width, (bot - top) / fig.height)
fr = fig.resize((int(fig.width * s), int(fig.height * s)), Image.LANCZOS)
img.paste(fr, ((W - fr.width) // 2, top + (bot - top - fr.height) // 2))
os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.save(OUT, optimize=True)
shutil.copy(OUT, "/Users/amey/Downloads/wtth-project-image.png")
print(line, os.path.getsize(OUT))
