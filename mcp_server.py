"""Minimal MCP server for Fish Speech S2 Pro GUI.

Exposes a small, focused tool set (6 tools) for TTS synthesis and voice-sample
preparation. Run alongside the Gradio app (start.bat) or standalone:

    uv pip install mcp[cli]
    .venv\\Scripts\\python.exe mcp_server.py

Then point your MCP client at this command. For HTTP/SSE transport see
`fastmcp run mcp_server.py --transport sse --host 127.0.0.1 --port 8765`.
"""

from __future__ import annotations

import os
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _NoopProgress:
    """Stand-in for gr.Progress() so handlers with a `progress` kwarg work."""

    def __call__(self, *args, **kwargs):
        return None


_app_module = None
_app_lock = threading.Lock()


def _app():
    """Lazy import of app.py so we don't pay the Gradio import cost at startup."""
    global _app_module
    if _app_module is None:
        with _app_lock:
            if _app_module is None:
                import app as _a  # noqa: WPS433
                _app_module = _a
    return _app_module


# ------- Prewarm: load + compile the PyTorch model in the background so the
# first synthesize_tts / transcribe_audio / etc. call is fast. We serialize
# every PyTorch-using tool behind _init_lock so a real MCP request blocks
# behind the warmup instead of double-initializing globals.

_init_lock = threading.Lock()
_prewarm_started = False
_prewarm_done = threading.Event()


def _start_prewarm():
    """Spawn a daemon thread that warms up the PyTorch model + codec."""
    global _prewarm_started
    with _init_lock:
        if _prewarm_started:
            return
        _prewarm_started = True

    def _run():
        print("[prewarm] Starting PyTorch model warmup...", flush=True)
        try:
            # Pick the first .wav in samples/ as a reference. If none, skip.
            samples_dir = os.path.join(ROOT, "samples")
            ref_path = None
            if os.path.isdir(samples_dir):
                for name in sorted(os.listdir(samples_dir)):
                    if name.lower().endswith(".wav"):
                        ref_path = os.path.join(samples_dir, name)
                        break
            if not ref_path or not os.path.isfile(ref_path):
                print("[prewarm] No WAV samples found; running warmup without clone_voice.", flush=True)
                with _init_lock:
                    try:
                        _app().generate_fish_python  # touch once to import lazily
                    except Exception as e:
                        print(f"[prewarm] import probe failed: {e}", flush=True)
                # Still load model via a minimal clone_voice path. Easier to skip if no ref.
                _prewarm_done.set()
                return

            with _init_lock:
                wav_path, message = _app().clone_voice(
                    trained_model_select="Base Model (Fish S2 Pro)",
                    text="warmup",
                    ref_audio=ref_path,
                    ref_text="",
                    top_p=0.8, top_k=50,
                    temp=0.7, rep_pen=1.1,
                    split_by_paragraph=False,
                    progress=_NoopProgress(),
                )
            print(f"[prewarm] Warmup clone_voice returned: {wav_path} / {message}", flush=True)
            # Clean up the throwaway output so outputs/ stays tidy.
            if wav_path and os.path.isfile(wav_path):
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
            print("[prewarm] Warmup complete.", flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[prewarm] Warmup failed: {e}", flush=True)
        finally:
            _prewarm_done.set()

    threading.Thread(target=_run, name="fish-prewarm", daemon=True).start()


try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write(
        "Missing dependency: 'mcp'. Install with:\n"
        "    uv pip install mcp[cli]\n"
        "or:\n"
        "    .venv\\Scripts\\python.exe -m pip install mcp\n"
    )
    sys.exit(1)


mcp = FastMCP("fish-speech-s2-pro")


@mcp.tool()
def list_samples() -> list[str]:
    """List the names of voice samples under samples/ that are ready for TTS."""
    return _app().get_sample_choices()


@mcp.tool()
def synthesize_tts(
    text: str,
    ref_audio_path: str,
    ref_text: str = "",
    trained_model: str = "Base Model (Fish S2 Pro)",
    top_p: float = 0.8,
    top_k: int = 50,
    temperature: float = 0.7,
    repetition_penalty: float = 1.1,
    split_by_paragraph: bool = True,
) -> str:
    """Synthesize speech from text using a reference voice sample (PyTorch engine).

    Args:
        text: Text to speak.
        ref_audio_path: Absolute path to a reference WAV/MP3 on disk.
        ref_text: Transcript of the reference audio (improves accuracy).
        trained_model: Trained LoRA name, or 'Base Model (Fish S2 Pro)'.
        top_p/top_k/temperature/repetition_penalty: Sampling knobs.
        split_by_paragraph: Split long text on blank lines.

    Returns:
        Absolute path of the generated WAV file under outputs/.
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    if not ref_audio_path or not os.path.isfile(ref_audio_path):
        raise FileNotFoundError(f"ref_audio_path not found: {ref_audio_path!r}")

    with _init_lock:
        wav_path, message = _app().clone_voice(
            trained_model_select=trained_model,
            text=text,
            ref_audio=ref_audio_path,
            ref_text=ref_text,
            top_p=top_p,
            top_k=top_k,
            temp=temperature,
            rep_pen=repetition_penalty,
            split_by_paragraph=split_by_paragraph,
            progress=_NoopProgress(),
        )
    if not wav_path:
        raise RuntimeError(message or "synthesis failed")
    return wav_path


@mcp.tool()
def transcribe_audio(
    audio_path: str,
    model_size: str = "small",
    language: str = "Auto",
) -> str:
    """Transcribe an audio file with Faster-Whisper. Returns the text.

    Args:
        audio_path: Absolute path to a WAV/MP3/FLAC file.
        model_size: tiny | base | small | medium | large-v3 (any faster-whisper size).
        language: One of WHISPER_LANGS keys (e.g. 'English', 'Auto') or 'Auto'.
    """
    if not audio_path or not os.path.isfile(audio_path):
        raise FileNotFoundError(f"audio_path not found: {audio_path!r}")
    return _app().transcribe_only(
        audio_path=audio_path,
        model_size=model_size,
        language_name=language,
        progress=_NoopProgress(),
    )


@mcp.tool()
def fix_audio(
    audio_path: str,
    normalize: bool = True,
    to_mono: bool = True,
) -> str:
    """Normalize volume / convert to mono in-place. Returns the path."""
    if not audio_path or not os.path.isfile(audio_path):
        raise FileNotFoundError(f"audio_path not found: {audio_path!r}")
    return _app().fix_audio_single(
        audio_path=audio_path,
        normalize=normalize,
        to_mono=to_mono,
    )


@mcp.tool()
def save_sample(
    audio_path: str,
    sample_name: str,
    transcription: str = "",
) -> str:
    """Save a prepared voice sample under samples/<sample_name>.

    Returns the saved sample name on success.
    """
    if not audio_path or not os.path.isfile(audio_path):
        raise FileNotFoundError(f"audio_path not found: {audio_path!r}")
    if not sample_name.strip():
        raise ValueError("sample_name must not be empty")
    message = _app().save_prep_sample(
        audio_path=audio_path,
        sample_name=sample_name,
        transcription=transcription,
    )
    if not message or "Error" in str(message) or "error" in str(message):
        raise RuntimeError(message or "save_prep_sample failed")
    return sample_name


@mcp.tool()
def delete_sample(sample_name: str) -> str:
    """Delete a saved voice sample by name."""
    if not sample_name.strip():
        raise ValueError("sample_name must not be empty")
    message = _app().delete_sample(sample_name=sample_name)
    if not message or "Error" in str(message):
        raise RuntimeError(message or "delete_sample failed")
    return sample_name


_DIALOGUE_MAX = 20  # Must match MAX_DIALOGUE_SEGMENTS in app.py


@mcp.tool()
def dialogue(
    segments: str,
    silence_duration: float = 0.5,
    trained_model: str = "Base Model (Fish S2 Pro)",
    top_p: float = 0.85,
    top_k: int = 70,
    temperature: float = 0.85,
    repetition_penalty: float = 1.05,
) -> str:
    """Synthesize a multi-speaker dialogue.

    Each segment is one utterance spoken by a saved voice sample.
    Sampling knobs apply uniformly to all segments.

    Args:
        segments: JSON-encoded list of {"sample": <name>, "text": <utterance>}
                  objects, OR a newline-delimited list of "<sample>\\|<text>" lines.
                  Up to 20 entries; each must reference a saved sample under samples/.
        silence_duration: Seconds of silence inserted between segments (0-5).
        trained_model: Trained LoRA name, or 'Base Model (Fish S2 Pro)'.
        top_p/top_k/temperature/repetition_penalty: Sampling knobs.

    Returns:
        Absolute path of the combined WAV file under outputs/.
    """
    import json

    raw = (segments or "").strip()
    if not raw:
        raise ValueError("segments must not be empty")

    parsed: list = []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"segments is not valid JSON: {e}") from e
        if not isinstance(parsed, list):
            raise ValueError("segments JSON must decode to a list")
    else:
        # Newline-delimited "sample|text" lines.
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" not in line:
                raise ValueError(f"line missing 'sample|text' separator: {line!r}")
            sample, text = line.split("|", 1)
            parsed.append({"sample": sample.strip(), "text": text.strip()})

    if not parsed:
        raise ValueError("segments must not be empty after parsing")
    if len(parsed) > _DIALOGUE_MAX:
        raise ValueError(f"too many segments ({len(parsed)}); max is {_DIALOGUE_MAX}")

    cleaned = []
    for i, seg in enumerate(parsed):
        if not isinstance(seg, dict):
            raise ValueError(f"segments[{i}] must be a dict with 'sample' and 'text'")
        sample = (seg.get("sample") or "").strip()
        text = (seg.get("text") or "").strip()
        if not sample:
            raise ValueError(f"segments[{i}].sample missing")
        if not text:
            raise ValueError(f"segments[{i}].text missing")
        cleaned.append((sample, text))

    pad = _DIALOGUE_MAX - len(cleaned)
    args_list: list = []
    args_list.extend([s for s, _ in cleaned])
    args_list.extend([None] * pad)
    args_list.extend([t for _, t in cleaned])
    args_list.extend([""] * pad)

    with _init_lock:
        wav_path, message = _app().generate_dialogue(
            trained_model,
            top_p,
            top_k,
            temperature,
            repetition_penalty,
            False,
            len(cleaned),
            float(silence_duration),
            *args_list,
            progress=_NoopProgress(),
        )
    if not wav_path:
        raise RuntimeError(message or "dialogue synthesis failed")
    return wav_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fish Speech S2 Pro MCP server")
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"),
                        default=os.environ.get("FISH_MCP_TRANSPORT", "streamable-http"))
    parser.add_argument("--host", default=os.environ.get("FISH_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("FISH_MCP_PORT", "8765")))
    parser.add_argument("--no-prewarm", action="store_true",
                        help="Skip the startup pre-warm of the PyTorch model.")
    args = parser.parse_args()

    if not args.no_prewarm:
        _start_prewarm()

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)
