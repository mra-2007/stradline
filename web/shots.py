"""Screenshots of the dashboard, for looking at the design with fresh eyes.

Drives the built app through a real browser (the system Chrome or Edge, so
nothing has to be downloaded) at both design sizes, and writes PNGs to
`web/shots/`. The API must already be running on :8000.

    venv\\Scripts\\python.exe web/shots.py
    venv\\Scripts\\python.exe web/shots.py --only intro

This is a development tool, not part of the product or of any gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
BASE = "http://127.0.0.1:8000"

DESKTOP = {"width": 1366, "height": 768}
PHONE = {"width": 390, "height": 844}

# Chromium needs a software GL in headless mode or WebGL — and so MapLibre and
# three.js — silently fails.
GL_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
]


def shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    # Software WebGL makes a frame expensive, so give the capture room; CSS
    # animations are frozen so a screenshot is not a blur.
    page.screenshot(path=str(path), timeout=180_000, animations="disabled")
    print(f"  wrote {path.relative_to(HERE.parent)}")


def settle(page, ms: int = 4000) -> None:
    page.wait_for_timeout(ms)


def scene_intro(page, suffix: str) -> None:
    page.goto(BASE, wait_until="load")
    page.wait_for_timeout(2500)
    shot(page, f"intro-{suffix}")
    button = page.get_by_role("button", name="Open the map")
    if button.count() > 0:
        button.first.click()
        page.wait_for_timeout(2600)
    shot(page, f"handover-{suffix}")


def scene_flyin(page, suffix: str) -> None:
    page.goto(f"{BASE}/?nointro", wait_until="load")
    # The fly-in is four seconds; a beat after it, everything is calm.
    page.wait_for_timeout(12000)
    shot(page, f"flyin-end-{suffix}")


def scene_layers(page, suffix: str) -> None:
    page.goto(f"{BASE}/?nointro", wait_until="load")
    settle(page, 14000)
    shot(page, f"case-smoke-{suffix}")


def scene_settlement(page, suffix: str, run: str = "case_study") -> None:
    page.goto(f"{BASE}/?nointro", wait_until="load")
    settle(page, 14000)
    strip_toggle = page.locator(".strip__more")
    if strip_toggle.is_visible():
        strip_toggle.click()
    page.select_option(".strip__foot select", run)
    settle(page, 12000)
    # Click the most cut-off settlement the sentence names, by clicking the
    # map where the settlements layer is densest: the centre of the view.
    found = False
    # Where the cut-off settlements cluster in the resting view.
    for x, y in [(800, 205), (760, 230), (845, 195), (700, 240), (880, 215), (640, 250)]:
        page.mouse.click(x, y)
        page.wait_for_timeout(1200)
        if page.locator(".settlement").count() > 0:
            found = True
            break
    if not found:
        print("  no settlement was hit by a click; skipping")
    settle(page, 1200)
    shot(page, f"settlement-{suffix}")


def scene_copilot(page, suffix: str, run: str = "case_study") -> None:
    page.goto(f"{BASE}/?nointro", wait_until="load")
    settle(page, 14000)
    strip_toggle = page.locator(".strip__more")
    if strip_toggle.is_visible():
        strip_toggle.click()
    page.select_option(".strip__foot select", run)
    settle(page, 12000)
    page.get_by_role("button", name="Ask about this area").click()
    page.wait_for_timeout(600)
    page.get_by_role("button", name="How many buildings are damaged?").click()
    # The copilot waits on the language model before falling back offline.
    page.wait_for_selector(".turn__answer", timeout=240_000)
    page.wait_for_timeout(600)
    figure = page.locator(".figure").first
    if figure.count() > 0:
        figure.click()
        page.wait_for_timeout(2000)
    trace = page.get_by_role("button", name="How this was answered")
    if trace.count() > 0:
        trace.first.click()
        page.wait_for_timeout(400)
    shot(page, f"copilot-{suffix}")


SCENES = {
    "intro": scene_intro,
    "flyin": scene_flyin,
    "layers": scene_layers,
    "settlement": scene_settlement,
    "copilot": scene_copilot,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", choices=sorted(SCENES), default=sorted(SCENES))
    parser.add_argument("--sizes", nargs="*", choices=["desktop", "phone"],
                        default=["desktop", "phone"])
    args = parser.parse_args()

    with sync_playwright() as play:
        browser = None
        for channel in ("chrome", "msedge", None):
            try:
                browser = play.chromium.launch(channel=channel, args=GL_ARGS)
                print(f"browser: {channel or 'bundled chromium'}")
                break
            except Exception as err:
                print(f"  {channel or 'bundled chromium'} unavailable: {str(err)[:90]}")
        if browser is None:
            print("No Chromium build was found. Install Chrome or Edge, or run "
                  "`playwright install chromium`.")
            return 1

        for size_name in args.sizes:
            size = DESKTOP if size_name == "desktop" else PHONE
            context = browser.new_context(viewport=size, device_scale_factor=1,
                                          reduced_motion="no-preference")
            page = context.new_page()
            page.on("console", lambda msg: (
                print(f"  console {msg.type}: {msg.text[:160]}")
                if msg.type in ("error", "warning") else None))
            page.on("requestfailed", lambda req: print(f"  request failed: {req.url[:140]}"))
            page.on("response", lambda res: (
                print(f"  {res.status} {res.url[:140]}") if res.status >= 400 else None))
            for scene in args.only:
                print(f"{scene} at {size['width']}x{size['height']}")
                SCENES[scene](page, size_name)
            context.close()

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
