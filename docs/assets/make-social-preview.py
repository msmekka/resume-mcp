from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
BG, CARD, BORDER = (13, 17, 23), (22, 27, 34), (48, 54, 61)
TEXT, MUTED, FAINT = (230, 237, 243), (139, 148, 158), (72, 79, 88)
ACCENT, GREEN = (88, 166, 255), (63, 185, 80)

HEL  = "/System/Library/Fonts/Helvetica.ttc"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MONO = "/System/Library/Fonts/Menlo.ttc"
def f(p, s): return ImageFont.truetype(p, s)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
d.rectangle([0, 0, W, 4], fill=ACCENT)

# ---------- left column, optically centred on y=320 ----------
d.text((72, 150), "resume-mcp", font=f(BOLD, 82), fill=TEXT)
for i, line in enumerate(["Turn a folder of resumes into a",
                          "searchable corpus your AI tools",
                          "can actually reason over."]):
    d.text((75, 256 + i * 40), line, font=f(HEL, 30), fill=MUTED)

x = 75
for c in ["find_resume", "retrieve_resume", "draft_resume"]:
    fo = f(MONO, 17)
    w = d.textlength(c, font=fo)
    d.rounded_rectangle([x, 404, x + w + 26, 442], radius=19,
                        fill=CARD, outline=BORDER, width=1)
    d.text((x + 13, 414), c, font=fo, fill=ACCENT)
    x += w + 36
d.text((75, 486), "MCP server  ·  Python  ·  MIT", font=f(MONO, 16), fill=MUTED)

# ---------- source documents, centred on the same axis ----------
def doc(x, y, label):
    d.rounded_rectangle([x, y, x + 100, y + 128], radius=8,
                        fill=CARD, outline=BORDER, width=2)
    for i in range(5):
        end = x + 100 - 22 - (20 if i == 4 else 0)
        d.rectangle([x + 16, y + 26 + i * 15, end, y + 29 + i * 15], fill=FAINT)
    d.text((x + 16, y + 102), label, font=f(MONO, 13), fill=MUTED)

doc(700, 182, "PDF")
doc(700, 330, "DOCX")

# arrow on the documents' shared axis
ay = 320
d.line([(818, ay), (872, ay)], fill=BORDER, width=4)
d.polygon([(872, ay - 9), (890, ay), (872, ay + 9)], fill=ACCENT)

# ---------- corpus panel ----------
PX, PY, PW, PH = 908, 160, 300, 320
d.rounded_rectangle([PX, PY, PX + PW, PY + PH], radius=12,
                    fill=CARD, outline=BORDER, width=2)
d.rounded_rectangle([PX + 18, PY + 20, PX + PW - 18, PY + 60], radius=8,
                    fill=BG, outline=BORDER, width=1)
cx, cy = PX + 38, PY + 40
d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], outline=MUTED, width=2)
d.line([(cx + 5, cy + 5), (cx + 11, cy + 11)], fill=MUTED, width=2)
d.text((PX + 56, PY + 30), "kubernetes", font=f(MONO, 17), fill=TEXT)

# only matches appear in results
ry = PY + 82
for name in ["Resume_Platform", "Resume_EngManager"]:
    d.rounded_rectangle([PX + 18, ry, PX + PW - 18, ry + 78], radius=8,
                        fill=BG, outline=BORDER, width=1)
    d.text((PX + 34, ry + 13), name, font=f(MONO, 15), fill=TEXT)
    d.rectangle([PX + 34, ry + 44, PX + 34 + 52, ry + 46], fill=GREEN)
    d.rectangle([PX + 92, ry + 44, PX + 34 + 156, ry + 46], fill=FAINT)
    d.rectangle([PX + 34, ry + 58, PX + 34 + 112, ry + 60], fill=FAINT)
    ry += 90
d.text((PX + 34, ry + 6), "2 of 26 variants", font=f(MONO, 14), fill=FAINT)

out = "/Users/msmekka/Documents/Next2026/resume-mcp-server/docs/assets/social-preview.png"

img.save(out)
print("wrote", img.size)
