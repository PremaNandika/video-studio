#!/usr/bin/env python3
"""ComfyUI workflow builders (API format) for the autoVSL local pipeline.

Two graphs, tuned for a 4 GB laptop GPU (RTX 3050 Ti):
  * build_txt2img       — SD 1.5 still image (rock solid on 4 GB)
  * build_animatediff   — SD 1.5 + AnimateDiff short clip -> mp4 (experimental)

Everything is emitted as the flat {node_id: {class_type, inputs}} dict that
ComfyUI's /prompt endpoint expects. Node class names were confirmed against
the live /object_info schema.
"""

from __future__ import annotations


def build_txt2img(
    *,
    checkpoint: str,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed",
    width: int = 512,
    height: int = 768,
    seed: int = 0,
    steps: int = 25,
    cfg: float = 7.0,
    sampler: str = "euler",
    scheduler: str = "normal",
    batch_size: int = 1,
    filename_prefix: str = "autovsl/still",
) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": batch_size}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0], "filename_prefix": filename_prefix}},
    }


def build_upscale(
    *,
    image_name: str,
    upscale_model: str = "RealESRGAN_x4plus.pth",
    filename_prefix: str = "autovsl/upscaled",
) -> dict:
    """Upscale an already-uploaded image with an ESRGAN model (e.g. 4x)."""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "UpscaleModelLoader",
              "inputs": {"model_name": upscale_model}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage",
              "inputs": {"images": ["3", 0], "filename_prefix": filename_prefix}},
    }


def build_inpaint(
    *,
    checkpoint: str,
    image_name: str,
    mask_name: str | None,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed",
    seed: int = 0,
    steps: int = 25,
    cfg: float = 7.0,
    denoise: float = 1.0,
    grow_mask_by: int = 6,
    sampler: str = "euler",
    scheduler: str = "normal",
    filename_prefix: str = "autovsl/inpaint",
) -> dict:
    """Inpaint the masked region of an uploaded image with a new prompt.
    If mask_name is None, the mask is taken from the image's own alpha channel
    (LoadImage output 1). Uses the base checkpoint (no dedicated inpaint model).
    """
    mask_src = ["1", 1] if mask_name is None else ["8", 0]
    wf = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["5", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["5", 1]}},
        "6": {"class_type": "VAEEncodeForInpaint",
              "inputs": {"pixels": ["1", 0], "vae": ["5", 2],
                         "mask": mask_src, "grow_mask_by": grow_mask_by}},
        "7": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": denoise, "model": ["5", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["6", 0]}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["5", 2]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": filename_prefix}},
    }
    if mask_name is not None:
        # a separate mask image → convert its red channel to a MASK
        wf["8"] = {"class_type": "ImageToMask",
                   "inputs": {"image": ["11", 0], "channel": "red"}}
        wf["11"] = {"class_type": "LoadImage", "inputs": {"image": mask_name}}
    return wf


def build_controlnet_keyframe(
    *,
    checkpoint: str,
    controlnet: str,
    control_image: str,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed",
    width: int = 512,
    height: int = 768,
    seed: int = 0,
    steps: int = 25,
    cfg: float = 7.0,
    strength: float = 0.8,
    sampler: str = "euler",
    scheduler: str = "normal",
    filename_prefix: str = "autovsl/keyframe",
) -> dict:
    """SD1.5 + ControlNet: generate an image guided by a control image
    (depth/pose/canny/lineart map) so composition stays consistent across a set.
    `control_image` is an already-uploaded image name."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": control_image}},
        "5": {"class_type": "ControlNetLoader",
              "inputs": {"control_net_name": controlnet}},
        "6": {"class_type": "ControlNetApplyAdvanced",
              "inputs": {"positive": ["2", 0], "negative": ["3", 0],
                         "control_net": ["5", 0], "image": ["4", 0],
                         "strength": strength, "start_percent": 0.0,
                         "end_percent": 1.0}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["6", 0], "negative": ["6", 1],
                         "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": filename_prefix}},
    }


def build_animatediff(
    *,
    checkpoint: str,
    motion_module: str,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed, jpeg artifacts",
    width: int = 384,
    height: int = 672,
    num_frames: int = 16,
    fps: int = 8,
    seed: int = 0,
    steps: int = 20,
    cfg: float = 7.5,
    sampler: str = "euler",
    scheduler: str = "normal",
    beta_schedule: str = "sqrt_linear (AnimateDiff)",
    context_length: int = 16,
    filename_prefix: str = "autovsl/clip",
    video_format: str = "video/h264-mp4",
) -> dict:
    """SD1.5 + AnimateDiff Gen1 -> VHS_VideoCombine mp4.

    Defaults are deliberately small (384x672, 16 frames) so a 4 GB card has a
    fighting chance. Scale down further if it OOMs; up if you have headroom.
    """
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "10": {"class_type": "ADE_AnimateDiffUniformContextOptions",
               "inputs": {"context_length": context_length, "context_stride": 1,
                          "context_overlap": 4,
                          "context_schedule": "uniform", "closed_loop": False,
                          "fuse_method": "flat", "use_on_equal_length": False,
                          "start_percent": 0.0, "guarantee_steps": 1}},
        "11": {"class_type": "ADE_AnimateDiffLoaderGen1",
               "inputs": {"model": ["1", 0], "model_name": motion_module,
                          "beta_schedule": beta_schedule,
                          "context_options": ["10", 0]}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": num_frames}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["11", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "VHS_VideoCombine",
              "inputs": {"images": ["6", 0], "frame_rate": float(fps),
                         "loop_count": 0, "filename_prefix": filename_prefix,
                         "format": video_format, "pingpong": False,
                         "save_output": True}},
    }
    return wf
