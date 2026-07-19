#!/usr/bin/env python3
"""HD pass for Wav2Lip output: restore each frame's face with GFPGAN so the soft
lip-synced mouth becomes sharp, detailed HD — then re-mux the original audio.

Local, GPU, free. Usage:
  python gfpgan_enhance.py --in <wav2lip.mp4> --out <final.mp4> [--blend 0.7]

--blend controls how much of the restored face is mixed back (1.0 = full GFPGAN,
0.6-0.8 keeps skin texture natural and reduces the "AI-beautified" look).
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import torch

HERE = Path(__file__).resolve().parent
GFPGAN_WEIGHT = HERE / "gfpgan_weights" / "GFPGANv1.4.pth"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--blend", type=float, default=0.75)
    ap.add_argument("--upscale", type=int, default=1)
    a = ap.parse_args()

    from gfpgan import GFPGANer
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"GFPGAN HD pass on {device.upper()} (blend={a.blend})", flush=True)
    restorer = GFPGANer(
        model_path=str(GFPGAN_WEIGHT), upscale=a.upscale, arch="clean",
        channel_multiplier=2, bg_upsampler=None, device=device,
    )

    cap = cv2.VideoCapture(a.src)
    if not cap.isOpened():
        sys.exit(f"cannot open {a.src}")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    outW, outH = W * a.upscale, H * a.upscale

    tmp = Path(a.out).with_suffix(".noaudio.mp4")
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{outW}x{outH}", "-r", f"{fps}",
         "-i", "pipe:0", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         "-pix_fmt", "yuv420p", str(tmp)],
        stdin=subprocess.PIPE,
    )

    i = enhanced = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        try:
            _, _, restored = restorer.enhance(
                frame, has_aligned=False, only_center_face=True, paste_back=True, weight=0.5)
        except Exception:
            restored = None
        if restored is None:
            out_frame = cv2.resize(frame, (outW, outH)) if a.upscale != 1 else frame
        else:
            if restored.shape[:2] != (outH, outW):
                restored = cv2.resize(restored, (outW, outH))
            base = cv2.resize(frame, (outW, outH)) if a.upscale != 1 else frame
            out_frame = cv2.addWeighted(restored, a.blend, base, 1 - a.blend, 0)
            enhanced += 1
        enc.stdin.write(np.ascontiguousarray(out_frame).tobytes())
        i += 1
        if i % 100 == 0:
            print(f"  HD {i}/{total} frames ({enhanced} faces restored)", flush=True)

    cap.release()
    enc.stdin.close()
    enc.wait()
    if enc.returncode != 0 or not tmp.is_file():
        sys.exit("HD encode failed")

    # re-mux the audio from the wav2lip output
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp), "-i", str(a.src),
         "-map", "0:v", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac",
         "-movflags", "+faststart", str(a.out)],
    )
    tmp.unlink(missing_ok=True)
    if r.returncode != 0 or not Path(a.out).is_file():
        sys.exit("HD mux failed")
    print(f"done: HD pass restored {enhanced}/{i} frames -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
