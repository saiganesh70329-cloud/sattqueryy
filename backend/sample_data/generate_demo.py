"""Generate a clearly synthetic image for offline pipeline demonstrations."""
from pathlib import Path
import random
import shutil
from PIL import Image, ImageDraw

random.seed(24)
out = Image.new('RGB', (512, 512), (83, 116, 61))
d = ImageDraw.Draw(out)
# Irregular agricultural parcels and woodland patches (illustrative, not remote sensing data).
for x in range(-30, 560, 74):
    for y in range(-30, 560, 68):
        c = random.choice([(93, 132, 65), (118, 139, 69), (65, 107, 62), (143, 139, 88)])
        d.polygon([(x+random.randrange(5,20),y+random.randrange(2,15)),(x+65,y),(x+70,y+55),(x+random.randrange(0,20),y+62)], fill=c)
# Curving river and a small built-up cluster.
d.line([(310,-10),(292,80),(325,145),(302,210),(337,270),(315,340),(360,425),(342,530)], fill=(46,111,156), width=28)
d.line([(310,-10),(292,80),(325,145),(302,210),(337,270),(315,340),(360,425),(342,530)], fill=(62,139,178), width=17)
for _ in range(190):
    x,y=random.randrange(120,270),random.randrange(210,360)
    d.rectangle((x,y,x+random.randrange(3,10),y+random.randrange(3,9)), fill=random.choice([(166,143,113),(194,173,135),(122,115,102)]))
for _ in range(140):
    x,y=random.randrange(512),random.randrange(512)
    r=random.randrange(2,6)
    d.ellipse((x-r,y-r,x+r,y+r), fill=random.choice([(37,82,47),(45,98,53),(55,108,61)]))
sample_dir = Path(__file__).parent
out.save(sample_dir/'demo_scene.png')
# A second synthetic date with a visible built-up expansion for change-screening demos.
later = out.copy()
ld = ImageDraw.Draw(later)
ld.polygon([(105,180),(176,174),(198,209),(181,263),(126,270),(97,231)], fill=(154,137,114))
for x in range(105,185,12):
    for y in range(187,260,12):
        ld.rectangle((x,y,x+7,y+6), fill=random.choice([(177,157,128),(191,169,138),(134,126,111)]))
later.save(sample_dir/'demo_scene_after.png')
public_dir = sample_dir.parents[1]/'public'
public_dir.mkdir(exist_ok=True)
shutil.copyfile(sample_dir/'demo_scene.png', public_dir/'demo_scene.png')
shutil.copyfile(sample_dir/'demo_scene_after.png', public_dir/'demo_scene_after.png')
