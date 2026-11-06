"""Make the app's celebration sounds with your ElevenLabs account (Sound Effects API), once.

Four levels by the profit of a winning close (frontend/src/celebrate.ts picks one clip of the level at
random, never the one it played last), four different clips each. The clips are saved in
frontend/public/sounds/celebrate with a manifest.json; after that they play from the app and cost no
more credits. A clip already on disk is kept unless --force, so a re-run only makes what is missing.

The key is read from ELEVENLABS_API_KEY - the environment, or backend/.env - and never printed.

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\make_celebration_sounds.py [--force] [--only small]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "public" / "sounds" / "celebrate"
API = "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128"

# key, smallest win in $ for the level, clip length in seconds, the prompts (one clip each).
TIERS = [
    ("small", 0, 2.0, [
        "a single bright magical coin chime with sparkles, short satisfying video game reward sound",
        "quick retro cash register ka-ching with a tiny coin jingle",
        "two bright bell notes rising, crisp success ding, clean and short",
        "short sparkling level-up blip, cheerful arcade reward",
    ]),
    ("nice", 10, 3.0, [
        "a small group of people cheering briefly with coins jingling",
        "short triumphant trumpet flourish with a sparkle shimmer on top",
        "arcade jackpot, a short cascade of coins falling onto a pile",
        "happy whoosh into a bright chime chord, upbeat success stinger",
    ]),
    ("big", 50, 4.0, [
        "crowd cheering and applauding over a triumphant brass fanfare",
        "slot machine jackpot bells ringing with coins pouring out",
        "fireworks popping and crackling with an excited crowd cheering",
        "victory orchestral hit with timpani followed by cheering",
    ]),
    ("huge", 150, 5.0, [
        "huge stadium crowd roaring and cheering while big fireworks explode",
        "epic orchestral victory fanfare with brass, timpani and choir, triumphant",
        "massive casino jackpot alarm, bells ringing, coins pouring, people celebrating",
        "grand fireworks finale with booming explosions and a crowd going wild",
    ]),
]


def api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    env = ROOT / "backend" / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "ELEVENLABS_API_KEY":
                key = value.strip().strip('"').strip("'")
    if not key:
        sys.exit("No ELEVENLABS_API_KEY: add the line ELEVENLABS_API_KEY=... to backend/.env")
    return key


def make(key: str, prompt: str, seconds: float) -> bytes:
    body = json.dumps({"text": prompt, "duration_seconds": seconds, "prompt_influence": 0.5}).encode()
    req = urllib.request.Request(API, data=body, method="POST",
                                 headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < 2:          # busy: wait and try again
                time.sleep(5 * (attempt + 1))
                continue
            raise SystemExit(f"ElevenLabs refused ({e.code}): {detail}") from None
    raise SystemExit("ElevenLabs kept answering 429 (too many requests); try again in a minute")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="make every clip again, even those on disk")
    ap.add_argument("--only", help="one level only: small, nice, big or huge")
    args = ap.parse_args()
    key = api_key()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, lo, secs, prompts in TIERS:
        files = []
        for i, prompt in enumerate(prompts, 1):
            f = OUT / f"{name}-{i}.mp3"
            if (args.only in (None, name)) and (args.force or not f.exists()):
                print(f"making {f.name} ({secs:g}s): {prompt}")
                f.write_bytes(make(key, prompt, secs))
            if f.exists():
                files.append(f.name)
        manifest.append({"key": name, "min": lo, "files": files})
    (OUT / "manifest.json").write_text(json.dumps({"tiers": manifest}, indent=1), encoding="utf-8")
    print(f"done: {sum(len(t['files']) for t in manifest)} clips in {OUT}")


if __name__ == "__main__":
    main()
