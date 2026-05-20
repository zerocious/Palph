"""
Pillow placeholder generator for pet assets.

Generates 125 PNGs (5 emotions × 5 colors × 5 accessories) + 5 emotion GIFs
into `assets/pet/`. Run once at build time; the bot reads these files at
runtime via `services.render_pet`.

Output filenames match what `render_pet` expects:
    assets/pet/<emotion>_<color>_<accessory>.png
    assets/pet/<emotion>.gif

This is placeholder art (programmer-art): colored circle body + emotion
emoji + small accessory shape. The Bot is fully functional with these
assets — UI works, customization picker shows real differences, level-up
shows actual previews. They just look ugly.

To replace with real artist's source assets later, rewrite the
`_compose_pet_png` function to:
  1. Load 5 grayscale pose PNGs from assets/pet/source/<emotion>.png
  2. Tint each pose with the color
  3. Composite the accessory overlay PNG on top
This script's overall structure (loop over emotions × colors × accessories)
stays the same.

Usage:
    python scripts/build_pet_assets.py
    # → writes 125 PNGs + 5 GIFs into <repo_root>/assets/pet/
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# ────────────────────────────────────────────────────────────────
# Catalogs — must match PetRepository.COLOR_CATALOG / ACCESSORY_CATALOG
# in repository.py. Mismatch here = generated files won't match what
# render_pet looks for.
# ────────────────────────────────────────────────────────────────
EMOTIONS = ("happy", "sad", "excited", "sleepy", "studying")

# Hex → RGB tuple for Pillow
COLOR_RGB = {
    "orange": (255, 140, 0),
    "grey":   (128, 128, 128),
    "blue":   (74, 144, 226),
    "green":  (80, 200, 120),
    "pink":   (255, 105, 180),
}

# Emotion → emoji rendered in the body's "face" area
EMOTION_EMOJI = {
    "happy":    "😊",
    "sad":      "😢",
    "excited":  "🤩",
    "sleepy":   "😴",
    "studying": "🤓",
}

# Accessory → drawing instruction (dict with shape/coords/color)
ACCESSORIES = ("none", "hat", "glasses", "scarf", "crown")


# ────────────────────────────────────────────────────────────────
# Drawing
# ────────────────────────────────────────────────────────────────
SIZE = 256              # final image side in px
CENTER = SIZE // 2
BODY_RADIUS = 80


def _font(size: int):
    """Try to load a system font that has emoji glyphs; fall back to default."""
    # Common emoji-capable fonts; absent on minimal containers but ok on Windows/Mac
    candidates = [
        "seguiemj.ttf",                  # Windows
        "/System/Library/Fonts/Apple Color Emoji.ttc",  # macOS
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",  # Linux
        "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_accessory(draw: ImageDraw.ImageDraw, accessory: str) -> None:
    """Overlay a small geometric accessory marker on top of the body."""
    if accessory == "none":
        return
    if accessory == "hat":
        # Triangle on top of head
        top = CENTER - BODY_RADIUS - 20
        draw.polygon(
            [(CENTER - 30, top + 30), (CENTER + 30, top + 30), (CENTER, top)],
            fill=(70, 50, 20),
        )
    elif accessory == "glasses":
        # Two circles where eyes would be
        eye_y = CENTER - 15
        for dx in (-22, 22):
            draw.ellipse(
                [(CENTER + dx - 15, eye_y - 12),
                 (CENTER + dx + 15, eye_y + 12)],
                outline=(20, 20, 20), width=4,
            )
        # Bridge
        draw.line(
            [(CENTER - 7, eye_y), (CENTER + 7, eye_y)],
            fill=(20, 20, 20), width=4,
        )
    elif accessory == "scarf":
        # Rectangle under the head/body
        draw.rectangle(
            [(CENTER - BODY_RADIUS + 10, CENTER + BODY_RADIUS - 20),
             (CENTER + BODY_RADIUS - 10, CENTER + BODY_RADIUS + 10)],
            fill=(180, 30, 60),
        )
    elif accessory == "crown":
        # Zigzag on top of head
        top = CENTER - BODY_RADIUS - 24
        points = []
        for i, x_off in enumerate(range(-40, 41, 16)):
            y = top if i % 2 == 0 else top + 24
            points.append((CENTER + x_off, y))
        # Close the bottom
        points.append((CENTER + 40, top + 28))
        points.append((CENTER - 40, top + 28))
        draw.polygon(points, fill=(255, 215, 0))


def _compose_pet_png(emotion: str, color: str, accessory: str) -> Image.Image:
    """Compose a single pet image from emotion + color + accessory."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))  # transparent BG
    draw = ImageDraw.Draw(img)

    # Body: filled circle in color
    body_color = COLOR_RGB[color]
    draw.ellipse(
        [(CENTER - BODY_RADIUS, CENTER - BODY_RADIUS),
         (CENTER + BODY_RADIUS, CENTER + BODY_RADIUS)],
        fill=body_color,
        outline=(0, 0, 0),
        width=3,
    )

    # Emotion emoji centered in the body
    emoji_font = _font(72)
    emoji_text = EMOTION_EMOJI[emotion]
    # textbbox is more reliable than textsize across Pillow versions
    try:
        bbox = draw.textbbox((0, 0), emoji_text, font=emoji_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except (AttributeError, TypeError):
        tw, th = 72, 72
    draw.text(
        (CENTER - tw // 2, CENTER - th // 2),
        emoji_text,
        font=emoji_font,
        embedded_color=True,
    )

    # Accessory overlay LAST so it sits on top of body
    _draw_accessory(draw, accessory)

    return img


def _compose_pet_gif(emotion: str) -> list:
    """Two-frame loop GIF: emotion in default color + accessory (orange + none)."""
    frame_a = _compose_pet_png(emotion, "orange", "none")
    # Frame B: slightly shifted vertically — fake "bounce"
    frame_b = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    frame_b.paste(frame_a, (0, -6), frame_a)
    return [frame_a, frame_b]


# ────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────
def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "assets" / "pet"
    out_dir.mkdir(parents=True, exist_ok=True)

    png_count = 0
    for emotion in EMOTIONS:
        for color in COLOR_RGB.keys():
            for accessory in ACCESSORIES:
                img = _compose_pet_png(emotion, color, accessory)
                fname = f"{emotion}_{color}_{accessory}.png"
                img.save(out_dir / fname)
                png_count += 1

    gif_count = 0
    for emotion in EMOTIONS:
        frames = _compose_pet_gif(emotion)
        # GIF doesn't support full alpha; export with white-ish background
        flat_frames = []
        for f in frames:
            bg = Image.new("RGBA", f.size, (255, 255, 255, 255))
            bg.alpha_composite(f)
            flat_frames.append(bg.convert("P", palette=Image.Palette.ADAPTIVE))
        flat_frames[0].save(
            out_dir / f"{emotion}.gif",
            save_all=True,
            append_images=flat_frames[1:],
            duration=400,
            loop=0,
            disposal=2,
        )
        gif_count += 1

    print(f"wrote {png_count} PNGs and {gif_count} GIFs to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
