#!/usr/bin/env python3
"""Render a recorded demo run as an animated terminal.

    Superseded by an actual asciinema recording (docs/media/demo.cast). Kept
    because it needs no recording tools and no cluster, which is why it existed.

The text is the verbatim output of a real `demo/run.sh` against a live k3d
cluster — nothing is re-enacted or retyped. The colours are kubemend's own,
taken from cli.py: the demo pipes its output through `sed`, so it runs with
--no-color and the terminal colours are stripped from the saved file. Painting
them back is showing what the command prints to a terminal, which is what a
reader would see if they ran it.
"""

import re
from PIL import Image, ImageDraw, ImageFont

SRC = "/tmp/demo-run.txt"
OUT = "/Users/srivatsakamballa/dev/kubemend/docs/media/demo.gif"
FONT = "/System/Library/Fonts/Menlo.ttc"
SIZE, ROWS, W, PAD = 15, 25, 1000, 22

BG, CHROME = (13, 16, 22), (22, 27, 34)
FG, DIM = (201, 209, 217), (110, 118, 129)
GREEN, RED, YELLOW, CYAN = (63, 185, 80), (248, 81, 73), (210, 153, 34), (86, 182, 194)

KEEP_FROM = "== 3. A release goes out"
DROP = ("== 6. The commit it produced", "== 7. The cluster, afterwards", "== Tearing down")
STOP_AFTER = "and reverted the one that did not."

HOLD = {
    "recovered after": 1600, "still failing after": 1500, "reverted in": 1300,
    "revert rate": 2400, "keeps coming back": 900, "watching for recovery": 800,
    "commit(s) written": 700, "== 8.": 1000, "== 10.": 800, "== Done": 900,
}


def colour_for(line: str):
    """kubemend's own scheme, from cli.py and demo/run.sh."""
    s = line.strip()
    if s.startswith("=="):                                   # demo/run.sh blue()
        return CYAN
    if s.startswith("✓"):                                    # AUTONOMY_COLOR[APPLY]
        return GREEN
    if s.startswith("✗"):
        return RED
    if s.startswith("-") and "image:" in s:                  # emission diff
        return RED
    if s.startswith("+") and "image:" in s:
        return GREEN
    if s.startswith("critical"):                             # SEV_COLOR[CRITICAL]
        return RED
    # Checked before the state rules: this is demo/run.sh's dim() narration,
    # not kubemend output, and it happens to contain "revert rate".
    if s.startswith("the revert rate above"):
        return DIM
    # The closing narration is one sentence across three lines; colouring only
    # the line containing "reverted" would split it down the middle.
    if s.startswith(("The agent never called", "It read the cluster", "and reverted the one")):
        return FG
    if "recovered after" in s or " verified " in s:
        return GREEN
    if "still failing after" in s:
        return RED
    # STATE_COLOR in cli.py; the log rows are prefixed with a timestamp, so
    # match the state as a word rather than at the start of the line.
    if "reverted in" in s or " reverted " in s or "revert rate" in s:
        return YELLOW
    if any(k in s for k in ("restored ", "commit ", "watching for", "undo:", "↳",
                            "incident log", "totals", "keeps coming back",
                            "policy 'conservative'", "two bad releases",
                            "the repository is back", "the revert rate above")):
        return DIM
    return None


def main():
    raw = open(SRC, encoding="utf-8", errors="replace").read().splitlines()
    plain_of = lambda l: re.sub(r"\033\[[0-9;]*m", "", l)

    start = next(i for i, l in enumerate(raw) if KEEP_FROM in plain_of(l))
    lines, skipping = [], False
    for line in raw[start:]:
        p = plain_of(line).rstrip()
        if any(s in p for s in DROP):
            skipping = True
            continue
        if skipping and p.strip().startswith("=="):
            skipping = False
        if skipping:
            continue
        lines.append(p)
        if STOP_AFTER in p:
            break

    font = ImageFont.truetype(FONT, SIZE)
    small = ImageFont.truetype(FONT, 12)
    lh, adv = SIZE + 6, 9.02
    H = PAD * 2 + ROWS * lh + 34

    frames, durations, buf = [], [], []
    for line in lines:
        buf.append(line)
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 30], fill=CHROME)
        for k, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([16 + k * 18, 11, 24 + k * 18, 19], fill=c)
        d.text((W // 2 - 62, 8), "demo/run.sh", font=small, fill=DIM)

        y = 34 + PAD
        for row in buf[-ROWS:]:
            d.text((PAD, y), row, font=font, fill=colour_for(row) or FG)
            y += lh
        frames.append(img)

        hold = next((ms for k, ms in HOLD.items() if k in line), None)
        durations.append(hold if hold else (240 if line.strip() else 90))

    durations[-1] = 4000
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    print(f"{OUT}\n  {len(frames)} frames  {W}x{H}")


if __name__ == "__main__":
    main()
