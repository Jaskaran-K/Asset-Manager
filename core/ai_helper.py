"""The AI tools at work (core.ai_tools), in a helper program of its own (AssetManager.exe --ai-helper), so a
graphics driver crash or running out of memory can't take the app with it (BiRefNet, tried for Remove
Background, crashed ONNX Runtime outright), and so the models stay loaded between requests.

It's core.model_reader's helper machinery: a named pipe with a random key, a job object that caps its memory
and ends it with the app, below-normal priority. It goes after IDLE_SECONDS without work. The app sends
pictures already made the models' size (uint8 arrays) and gets back numbers:
- "pictures": SigLIP vectors (VECTOR_SIZE, float16, unit length) for PICTURE_SIDE squares;
- "text": the vector of a search's words (SigLIP's text padded to 64 with "</s>", as it was trained: padded
  with "<pad>" its results were much worse);
- "cutout": a mask (0-255) for a CUTOUT_SIDE square (the picture squashed to it), by the model asked for
  (ai_tools.CUTOUT_MODELS: BiRefNet, or BEN2).

Each model runs on the graphics card through DirectML when it can (graph optimisation "all": "basic" gave
wrong text vectors there), else on the processor ("basic": "all" can't load the fp16 models there). Only one
cut-out model is kept loaded: with BiRefNet's working memory on an RTX 4070, BEN2 took 4.2 s a picture instead
of 0.3 s (crowded out of the card's memory), so choosing the other one unloads the first.

ENGINE_ENV="fake" uses a stand-in engine (tests: no models needed): picture vectors from the mean colour,
text vectors from colour words, masks from the corner colour. "inprocess-fake" runs it in the app.
"""

from __future__ import annotations

import os
import sys
import threading
import zlib
from typing import Any, Optional

import numpy as np

from . import ai_tools

PICTURE_SIDE = 224
CUTOUT_SIDE = 1024
VECTOR_SIZE = 768
TEXT_TOKENS = 64
PICTURE_BATCH = 16  # pictures per request (a search waits at most for one batch)
IDLE_SECONDS = 300.0  # (loading the models again takes a few seconds)
TIMEOUT = 300.0  # seconds for one request (the first loads its model)
ENGINE_ENV = "ASSETMANAGER_AI"  # "fake" / "inprocess-fake": the stand-in engine; "cpu": the processor only


class AIFailed(Exception):
    """The helper couldn't do it (the message says why, for the user)."""


# ----------------------------------------------------------------------
# The helper's side
# ----------------------------------------------------------------------
class Engine:
    """The models, loaded when first used."""

    def __init__(self):
        ai_tools.add_to_path()
        import onnxruntime  # noqa: PLC0415

        self.ort = onnxruntime
        self._sessions: dict[str, Any] = {}
        self.devices: dict[str, str] = {}
        self._tokenizer = None

    def _session(self, key: str, file: str):
        name = f"{key}/{file}"
        if name in self._sessions:
            return self._sessions[name]
        path = str(ai_tools.model_folder(key) / file)
        ort = self.ort
        session = None
        if os.environ.get(ENGINE_ENV) != "cpu" and "DmlExecutionProvider" in ort.get_available_providers():
            options = ort.SessionOptions()
            options.log_severity_level = 3
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            try:
                session = ort.InferenceSession(path, options, providers=["DmlExecutionProvider"])
                self.devices[key] = "graphics card"
            except Exception:  # pylint: disable=broad-except
                session = None  # (no DirectX 12 card, or the driver refused: the processor)
        if session is None:
            options = ort.SessionOptions()
            options.log_severity_level = 3
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
            self.devices[key] = "processor"
        self._sessions[name] = session
        return session

    def pictures(self, pixels: np.ndarray) -> np.ndarray:
        session = self._session("search", "vision_model_fp16.onnx")
        x = (pixels.astype(np.float32) / 127.5 - 1.0).transpose(0, 3, 1, 2)  # (SigLIP: mean 0.5, std 0.5)
        out = np.asarray(session.run(["pooler_output"], {"pixel_values": np.ascontiguousarray(x)})[0], np.float32)
        return _unit(out).astype(np.float16)

    def text(self, words: str) -> np.ndarray:
        if self._tokenizer is None:
            from tokenizers import Tokenizer  # noqa: PLC0415

            self._tokenizer = Tokenizer.from_file(str(ai_tools.model_folder("search") / "tokenizer.json"))
        tokenizer = self._tokenizer
        end = tokenizer.token_to_id("</s>")
        ids = (tokenizer.encode(words).ids + [end] * TEXT_TOKENS)[:TEXT_TOKENS]
        session = self._session("search", "text_model_fp16.onnx")
        out = np.asarray(session.run(["pooler_output"], {"input_ids": np.array([ids], np.int64)})[0], np.float32)
        return _unit(out)[0]

    def cutout(self, pixels: np.ndarray, model: str) -> np.ndarray:
        tool = ai_tools.TOOLS[model]
        for other in ai_tools.CUTOUT_MODELS:  # (one at a time: see the module docstring)
            if other != model:
                for name in [n for n in self._sessions if n.startswith(f"{other}/")]:
                    del self._sessions[name]
        session = self._session(model, tool.files[0].name)
        x = pixels.astype(np.float32) / 255.0
        if tool.normalise:  # (BiRefNet: ImageNet's mean and spread; BEN2 takes 0..1)
            x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        x = x.transpose(2, 0, 1)[None]
        name = session.get_inputs()[0].name
        out = np.asarray(session.run(None, {name: np.ascontiguousarray(x)})[0], np.float32).squeeze()
        if out.min() < -0.01 or out.max() > 1.01:  # (logits)
            out = 1.0 / (1.0 + np.exp(-out))
        return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


_FAKE_COLOURS = {"red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1), "white": (1, 1, 1), "black": (0, 0, 0),
                 "yellow": (1, 1, 0), "grey": (0.5, 0.5, 0.5)}


class FakeEngine:
    """A stand-in for the models (tests): a picture's vector is its mean colour, text's the colour named in
    it (or a direction from its words), a mask is what differs from the top-left corner's colour."""

    devices = {"search": "stand-in", "birefnet": "stand-in", "ben2": "stand-in"}

    @staticmethod
    def _colour_vector(rgb) -> np.ndarray:
        v = np.zeros(VECTOR_SIZE, np.float32)
        v[:3] = np.asarray(rgb, np.float32) - 0.5
        v[3] = 0.05
        return _unit(v[None])[0]

    def pictures(self, pixels: np.ndarray) -> np.ndarray:
        means = pixels.reshape(len(pixels), -1, 3).mean(axis=1) / 255.0
        return np.stack([self._colour_vector(m) for m in means]).astype(np.float16)

    def text(self, words: str) -> np.ndarray:
        for word in words.lower().split():
            if word in _FAKE_COLOURS:
                return self._colour_vector(_FAKE_COLOURS[word])
        v = np.random.default_rng(zlib.crc32(words.encode("utf-8"))).standard_normal(VECTOR_SIZE).astype(np.float32)
        return _unit(v[None])[0]

    def cutout(self, pixels: np.ndarray, model: str) -> np.ndarray:
        corner = pixels[0, 0].astype(np.int16)
        return np.where(np.abs(pixels.astype(np.int16) - corner).sum(axis=2) > 30, 255, 0).astype(np.uint8)


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)


_engine: Optional[Any] = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = FakeEngine() if os.environ.get(ENGINE_ENV, "").endswith("fake") else Engine()
    return _engine


def handle(request: dict[str, Any]) -> dict[str, Any]:
    """Do one request (in the helper)."""
    op = request.get("op")
    if op == "ping":
        return {"pong": os.getpid()}
    engine = _get_engine()
    if op == "pictures":
        return {"vectors": engine.pictures(np.asarray(request["pixels"], np.uint8)), "device": engine.devices.get("search")}
    if op == "text":
        return {"vector": engine.text(str(request["words"])), "device": engine.devices.get("search")}
    if op == "cutout":
        model = request.get("model", ai_tools.CUTOUT_MODELS[0])
        return {"mask": engine.cutout(np.asarray(request["pixels"], np.uint8), model), "device": engine.devices.get(model)}
    return {"error": f"unknown request {op!r}"}


def serve(address: str) -> int:
    from .model_reader import serve as serve_requests

    return serve_requests(address, handle, IDLE_SECONDS)


# ----------------------------------------------------------------------
# The app's side
# ----------------------------------------------------------------------
def helper_command(address: str) -> list[str]:
    """How to start the helper: the app itself with --ai-helper (from source: this module)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ai-helper", address]
    return [sys.executable, "-m", "core.ai_helper", address]


_helper = None
_helper_lock = threading.Lock()
last_device: dict[str, str] = {}  # what each tool last ran on ("graphics card", "processor"), for the windows


def _reader():
    global _helper
    with _helper_lock:
        if _helper is None:
            from .model_reader import ModelReader

            _helper = ModelReader("ai", background=True, command=helper_command,
                                  extra_env=lambda: {ai_tools.ROOT_ENV: str(ai_tools.root())})
        return _helper


def close() -> None:
    """End the helper (the app is closing, or a model was removed)."""
    with _helper_lock:
        helper = _helper
    if helper is not None:
        helper.close()


def _ask(request: dict[str, Any], tool: str) -> dict[str, Any]:
    if os.environ.get(ENGINE_ENV) == "inprocess-fake":
        reply = handle(request)
    else:
        if not ai_tools.is_installed(tool) and not os.environ.get(ENGINE_ENV, "").endswith("fake"):
            raise AIFailed(f"{ai_tools.TOOLS[tool].name} isn't downloaded.")
        from .model_reader import ReaderUnavailable, ReadFailed

        try:
            reply = _reader().request(request, TIMEOUT)
        except ReadFailed as exc:
            message = str(exc).replace("reading it crashed the model reader", "the AI helper crashed")
            raise AIFailed(f"{ai_tools.TOOLS[tool].name} failed: {message}.") from None
        except ReaderUnavailable as exc:
            raise AIFailed(f"The AI helper couldn't start: {exc}") from None
    if "error" in reply:
        raise AIFailed(f"{ai_tools.TOOLS[tool].name} failed: {reply['error']}")
    if reply.get("device"):
        last_device[tool] = reply["device"]
    return reply


def picture_vectors(pixels: np.ndarray) -> np.ndarray:
    """SigLIP vectors (n, VECTOR_SIZE) float16 for (n, PICTURE_SIDE, PICTURE_SIDE, 3) uint8 pictures."""
    return np.asarray(_ask({"op": "pictures", "pixels": np.ascontiguousarray(pixels, np.uint8)}, "search")["vectors"])


def text_vector(words: str) -> np.ndarray:
    """The vector (VECTOR_SIZE,) float32 of a search's words."""
    return np.asarray(_ask({"op": "text", "words": words}, "search")["vector"], np.float32)


def cutout_mask(pixels: np.ndarray, model: str = ai_tools.CUTOUT_MODELS[0]) -> np.ndarray:
    """A cut-out model's mask (CUTOUT_SIDE square, uint8) for a (CUTOUT_SIDE, CUTOUT_SIDE, 3) uint8 picture."""
    request = {"op": "cutout", "model": model, "pixels": np.ascontiguousarray(pixels, np.uint8)}
    return np.asarray(_ask(request, model)["mask"])


if __name__ == "__main__":  # (from source: python -m core.ai_helper ADDRESS)
    sys.exit(serve(sys.argv[1]))
