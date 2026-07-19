#!/usr/bin/env python3
"""Minimal ComfyUI HTTP API client (stdlib only, no extra deps).

Talks to a locally running ComfyUI (default 127.0.0.1:8188). Submits a
workflow in API format, polls until it finishes, and pulls the resulting
files off the server. Used by generate-video-local.py so autoVSL agents can
drive the free local GPU pipeline the same way they drive fal.ai.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


class ComfyUIError(RuntimeError):
    pass


class ComfyUIClient:
    def __init__(self, server: str = "127.0.0.1:8188", timeout: int = 15):
        self.server = server.replace("http://", "").rstrip("/")
        self.base = f"http://{self.server}"
        self.timeout = timeout

    # --- low level ---------------------------------------------------------
    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
            return r.read()

    def _get_json(self, path: str) -> dict:
        return json.loads(self._get(path))

    def ping(self) -> bool:
        try:
            self._get("/system_stats")
            return True
        except Exception:
            return False

    # --- workflow lifecycle ------------------------------------------------
    def submit(self, workflow: dict) -> str:
        """POST a workflow (API format). Returns the prompt_id."""
        payload = json.dumps({"prompt": workflow}).encode()
        req = urllib.request.Request(
            self.base + "/prompt", data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise ComfyUIError(f"ComfyUI rejected the workflow: {detail}") from e
        if "prompt_id" not in resp:
            raise ComfyUIError(f"No prompt_id in response: {resp}")
        return resp["prompt_id"]

    def wait(self, prompt_id: str, poll: float = 2.0, max_wait: float = 1800.0) -> dict:
        """Block until the prompt is done. Returns its history entry."""
        waited = 0.0
        while waited < max_wait:
            hist = self._get_json(f"/history/{prompt_id}")
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("completed") or status.get("status_str") == "success":
                    return entry
                if status.get("status_str") == "error":
                    raise ComfyUIError(f"Execution error: {json.dumps(status)[:800]}")
            time.sleep(poll)
            waited += poll
        raise ComfyUIError(f"Timed out after {max_wait}s waiting for {prompt_id}")

    def outputs(self, history_entry: dict) -> list[dict]:
        """Flatten all produced files (images, gifs, videos) from a run."""
        files: list[dict] = []
        for node_out in history_entry.get("outputs", {}).values():
            for key in ("images", "gifs", "videos"):
                for f in node_out.get(key, []):
                    files.append(f)
        return files

    def download(self, file_info: dict, dest: Path) -> Path:
        """Fetch one output file to dest via /view."""
        q = urllib.parse.urlencode({
            "filename": file_info["filename"],
            "subfolder": file_info.get("subfolder", ""),
            "type": file_info.get("type", "output"),
        })
        data = self._get(f"/view?{q}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def run(self, workflow: dict, poll: float = 2.0, max_wait: float = 1800.0) -> list[dict]:
        """Submit + wait + return output file descriptors."""
        pid = self.submit(workflow)
        entry = self.wait(pid, poll=poll, max_wait=max_wait)
        return self.outputs(entry)

    def upload_image(self, path, subfolder: str = "") -> str:
        """Upload a local image into ComfyUI's input/ dir via /upload/image.
        Returns the server-side name to feed a LoadImage node."""
        path = Path(path)
        data = path.read_bytes()
        boundary = "----comfyuistudio7c3f"
        parts = []
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode())
        parts.append(data)
        parts.append(f"\r\n--{boundary}\r\n".encode())
        parts.append('Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode())
        if subfolder:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="subfolder"\r\n\r\n{subfolder}\r\n'.encode())
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base + "/upload/image", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read())
        name = resp["name"]
        sf = resp.get("subfolder", "")
        return f"{sf}/{name}" if sf else name

    # --- introspection helpers --------------------------------------------
    def checkpoints(self) -> list[str]:
        info = self._get_json("/object_info/CheckpointLoaderSimple")
        return info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]

    def motion_modules(self) -> list[str]:
        info = self._get_json("/object_info/ADE_AnimateDiffLoaderGen1")
        return info["ADE_AnimateDiffLoaderGen1"]["input"]["required"]["model_name"][0]

    def upscale_models(self) -> list[str]:
        info = self._get_json("/object_info/UpscaleModelLoader")
        return info["UpscaleModelLoader"]["input"]["required"]["model_name"][0]

    def controlnets(self) -> list[str]:
        info = self._get_json("/object_info/ControlNetLoader")
        return info["ControlNetLoader"]["input"]["required"]["control_net_name"][0]


if __name__ == "__main__":
    c = ComfyUIClient()
    if not c.ping():
        raise SystemExit("ComfyUI not reachable at 127.0.0.1:8188 — start it first.")
    print("ComfyUI up.")
    print("checkpoints:", c.checkpoints())
    print("motion modules:", c.motion_modules())
