#!/usr/bin/env python3
"""Generate the README's terminal media.

Committed so the images are reproducible rather than mystery binaries: run it
and you get byte-comparable output. The text shown is copied from real runs of
``examples/demo.py`` -- nothing here is mocked up prettier than the tool is.

    pip install Pillow
    python docs/make_media.py

Writes to docs/media/:
    diff.gif      animated: the drift diff catching a vendor change
    inspect.png   static:   the inspect table
    report.png    static:   the "not observed" section of a report
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "media"

# -- terminal look ---------------------------------------------------------

BG = (12, 14, 18)
PANEL = (22, 24, 29)
CHROME = (30, 33, 40)
BORDER = (44, 48, 57)

FG = (205, 209, 217)
DIM = (125, 133, 148)
BRIGHT = (236, 238, 242)
GREEN = (126, 198, 153)
RED = (242, 119, 122)
BLUE = (111, 179, 210)
AMBER = (255, 204, 102)

FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
FONT_SIZE = 15
LINE_H = 22
PAD_X = 22
PAD_TOP = 46
PAD_BOTTOM = 18
DOT_R = 5

font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
CHAR_W = font.getlength("M")


def _panel(cols: int, rows: int, title: str):
    """Blank terminal window sized to a character grid."""
    w = int(PAD_X * 2 + cols * CHAR_W)
    h = int(PAD_TOP + rows * LINE_H + PAD_BOTTOM)
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=10, fill=PANEL, outline=BORDER)
    d.rounded_rectangle([0, 0, w - 1, 32], radius=10, fill=CHROME)
    d.rectangle([0, 22, w - 1, 33], fill=CHROME)
    d.line([0, 33, w - 1, 33], fill=BORDER)

    for i, colour in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        cx = 18 + i * 18
        d.ellipse([cx - DOT_R, 16 - DOT_R, cx + DOT_R, 16 + DOT_R], fill=colour)

    tw = d.textlength(title, font=font)
    d.text(((w - tw) / 2, 9), title, font=font, fill=DIM)
    return img, d


def _draw_lines(d, lines, limit_chars=None):
    """Draw ``lines`` -- each a list of (text, colour) spans.

    ``limit_chars`` reveals only the first N characters overall, which is what
    produces the typing and line-by-line effect.
    """
    budget = limit_chars
    for row, spans in enumerate(lines):
        x = PAD_X
        y = PAD_TOP + row * LINE_H
        for text, colour in spans:
            if budget is not None:
                if budget <= 0:
                    return
                if len(text) > budget:
                    text = text[:budget]
                budget -= len(text)
            if text:
                d.text((x, y), text, font=font, fill=colour)
            x += d.textlength(text, font=font)


def render(lines, title, cols=None, rows=None):
    cols = cols or max(sum(len(t) for t, _ in spans) for spans in lines) + 2
    rows = rows or len(lines)
    img, d = _panel(cols, rows, title)
    _draw_lines(d, lines)
    return img


def save_gif(frames, durations, path):
    """Write an animated GIF, palettised to keep it small."""
    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=32) for f in frames]
    pal[0].save(
        path,
        save_all=True,
        append_images=pal[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return path


# -- content (verbatim from examples/demo.py output) -----------------------

PROMPT = [("$ ", GREEN)]

DIFF_CMD = "apirecorder diff recordings/billing recordings/billing-ci"

DIFF_OUT = [
    [("", FG)],
    [("14 change(s) against baseline ", FG), ("(4 breaking, 3 additive, 7 info)", DIM)],
    [("", FG)],
    [("BREAKING", RED), ("  POST /v1/subscriptions", BRIGHT)],
    [("          primary status 201 -> 202", FG)],
    [("BREAKING", RED), ("  POST /v1/subscriptions ", BRIGHT), ("201->202", AMBER)],
    [("          ", FG), ("`current_period_end`", AMBER), (" (number) is gone", FG)],
    [("BREAKING", RED), ("  GET /v1/subscriptions ", BRIGHT), ("200", AMBER)],
    [("          ", FG), ("`has_more`", AMBER), (" (boolean) is gone", FG)],
    [("          ", FG), ("`data[].plan`", AMBER), (" is now sometimes null", FG)],
    [("", FG)],
    [("ADDITIVE", BLUE), ("  GET /v1/subscriptions ", BRIGHT), ("200", AMBER)],
    [("          ", FG), ("`next_cursor`", AMBER), (" (string) added", FG)],
    [("ADDITIVE", BLUE), ("  GET /v1/subscriptions/{id} ", BRIGHT), ("200", AMBER)],
    [("          ", FG), ("`cancel_at`", AMBER), (" (null) added", FG)],
    [("", FG)],
    [("$ ", GREEN), ("echo $?", BRIGHT)],
    [("1", RED), ("   ", FG), ("# CI fails on a breaking change", DIM)],
]

INSPECT = [
    [("$ ", GREEN), ("apirecorder inspect recordings/billing", BRIGHT)],
    [("", FG)],
    [("subscription_lifecycle", BRIGHT), (" - 5 interaction(s), 1.4s total", FG)],
    [("", FG)],
    [("#  method  endpoint                status  ms   note", DIM)],
    [("-  ------  ----------------------  ------  ---  ----", DIM)],
    [("1  POST    /v1/subscriptions       ", FG), ("201", GREEN), ("     312", FG)],
    [("2  GET     /v1/subscriptions/{id}  ", FG), ("200", GREEN), ("     88", FG)],
    [("3  PATCH   /v1/subscriptions/{id}  ", FG), ("200", GREEN), ("     201", FG)],
    [("4  GET     /v1/subscriptions       ", FG), ("200", GREEN), ("     143", FG)],
    [
        ("5  DELETE  /v1/subscriptions/{id}  ", FG),
        ("204", GREEN),
        ("     96   ", FG),
        ("empty", DIM),
    ],
]

REPORT = [
    [("$ ", GREEN), ("apirecorder report recordings/billing", BRIGHT)],
    [("", FG)],
    [("## Not observed", BRIGHT)],
    [("", FG)],
    [("- No 429 recorded anywhere - any rate-limit or retry", FG)],
    [("  path never ran.", FG)],
    [("- No 401 or 403 recorded anywhere - auth failure and", FG)],
    [("  token refresh paths never ran.", FG)],
    [("- The last recorded response from ", FG), ("GET /v1/subscriptions", AMBER)],
    [("  still had ", FG), ("has_more", AMBER), (" set, so the final page was", FG)],
    [("  never fetched.", FG)],
    [("- ", FG), ("trial_end", AMBER), (" was null in every recorded response -", FG)],
    [("  its real type is unknown.", FG)],
    [("- Nothing written by ", FG), ("/v1/subscriptions", AMBER), (" was ever read", FG)],
    [("  back with a GET, so the persisted shape is", FG)],
    [("  unverified.", FG)],
]


def build_diff_gif():
    cols = max(sum(len(t) for t, _ in spans) for spans in DIFF_OUT) + 4
    rows = 1 + len(DIFF_OUT)
    title = "apirecorder"

    frames, durations = [], []

    def frame(limit, ms):
        img, d = _panel(cols, rows, title)
        _draw_lines(d, [PROMPT + [(DIFF_CMD, BRIGHT)]] + DIFF_OUT, limit_chars=limit)
        frames.append(img)
        durations.append(ms)

    # 1. type the command
    typed = len("$ ")
    frame(typed, 500)
    for i in range(2, len(DIFF_CMD) + 1, 3):
        frame(typed + i, 35)
    cmd_chars = typed + len(DIFF_CMD)
    frame(cmd_chars, 450)

    # 2. reveal the output a line at a time
    running = cmd_chars
    for spans in DIFF_OUT:
        running += sum(len(t) for t, _ in spans)
        blank = not any(t.strip() for t, _ in spans)
        frame(running, 90 if blank else 230)

    # 3. hold on the finished screen
    frame(running, 3600)
    return save_gif(frames, durations, OUT / "diff.gif")


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    gif = build_diff_gif()
    render(INSPECT, "apirecorder inspect").save(OUT / "inspect.png", optimize=True)
    render(REPORT, "apirecorder report").save(OUT / "report.png", optimize=True)

    for path in (gif, OUT / "inspect.png", OUT / "report.png"):
        size = os.path.getsize(path)
        with Image.open(path) as im:
            print("%-28s %5.0f KB  %dx%d" % (path.name, size / 1024, *im.size))


if __name__ == "__main__":
    main()
