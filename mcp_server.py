"""Minimal MCP server for Fish Speech S2 Pro GUI.

Exposes a small, focused tool set (7 tools) for TTS synthesis and voice-sample
preparation. Run alongside the Gradio app (start.bat) or standalone:

    uv pip install mcp[cli]
    set FISH_MCP_PASSWORD=<strong password>
    .venv\\Scripts\\python.exe mcp_server.py

For internet exposure, keep this server bound to 127.0.0.1 and put Cloudflare
Tunnel in front of it. Clients must send HTTP Basic auth:

    Authorization: Basic base64(fish:<FISH_MCP_PASSWORD>)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import os
import re
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SAMPLES_DIR = os.path.join(ROOT, "samples")
OUTPUTS_DIR = os.path.join(ROOT, "outputs")
ALLOWED_AUDIO_DIRS = (SAMPLES_DIR, OUTPUTS_DIR)
SUPPORTED_AUDIO_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
SAMPLE_NAME_RE = re.compile(r"^[A-Za-z0-9_ -]{1,80}$")


class BasicAuthMiddleware:
    """Small ASGI Basic Auth middleware for Streamable HTTP/SSE transports."""

    def __init__(self, app, username: str, password: str):
        self.app = app
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="fish-mcp"'),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"Unauthorized"})

    def _authorized(self, scope) -> bool:
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        prefix = b"basic "
        if not auth.lower().startswith(prefix):
            return False
        try:
            decoded = base64.b64decode(auth[len(prefix) :], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        username, sep, password = decoded.partition(":")
        if not sep:
            return False
        return hmac.compare_digest(username.encode("utf-8"), self.username) and hmac.compare_digest(
            password.encode("utf-8"), self.password
        )


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


def _clean_sample_name(sample_name: str) -> str:
    name = (sample_name or "").strip()
    if not name:
        raise ValueError("sample_name must not be empty")
    if not SAMPLE_NAME_RE.fullmatch(name):
        raise ValueError("sample_name may only contain letters, numbers, spaces, underscores, and hyphens")
    return name.replace(" ", "_")


def _sample_audio_path(sample_name: str) -> str:
    name = _clean_sample_name(sample_name)
    path = os.path.abspath(os.path.join(SAMPLES_DIR, f"{name}.wav"))
    if not path.startswith(os.path.abspath(SAMPLES_DIR) + os.sep):
        raise ValueError("sample path escaped samples directory")
    return path


def _safe_audio_path(path: str, *, must_exist: bool = True, writable: bool = False) -> str:
    if not path:
        raise ValueError("audio path must not be empty")

    absolute = os.path.abspath(path)
    allowed = [os.path.abspath(d) + os.sep for d in ALLOWED_AUDIO_DIRS]
    in_allowed_dir = any(absolute.startswith(root) for root in allowed)
    if not in_allowed_dir:
        raise ValueError("audio path must be under samples/ or outputs/")
    if os.path.splitext(absolute)[1].lower() not in SUPPORTED_AUDIO_EXTS:
        raise ValueError(f"audio path must end with one of: {', '.join(SUPPORTED_AUDIO_EXTS)}")
    if must_exist and not os.path.isfile(absolute):
        raise FileNotFoundError(f"audio path not found: {absolute!r}")
    if writable and not os.access(absolute, os.W_OK):
        raise PermissionError(f"audio path is not writable: {absolute!r}")
    return absolute


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
            ref_path = None
            if os.path.isdir(SAMPLES_DIR):
                for name in sorted(os.listdir(SAMPLES_DIR)):
                    if name.lower().endswith(".wav"):
                        ref_path = os.path.join(SAMPLES_DIR, name)
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
def sample_path(sample_name: str) -> str:
    """Return the absolute WAV path for a saved sample name."""
    path = _sample_audio_path(sample_name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"sample not found: {sample_name!r}")
    return path


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
    ref_audio_path = _safe_audio_path(ref_audio_path)

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
    audio_path = _safe_audio_path(audio_path)
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
    audio_path = _safe_audio_path(audio_path, writable=True)
    fixed_path, message = _app().fix_audio_single(
        audio_path=audio_path,
        normalize=normalize,
        to_mono=to_mono,
    )
    if str(message).lower().startswith("error"):
        raise RuntimeError(message)
    return fixed_path


@mcp.tool()
def save_sample(
    audio_path: str,
    sample_name: str,
    transcription: str = "",
) -> str:
    """Save a prepared voice sample under samples/<sample_name>.

    Returns the saved sample name on success.
    """
    audio_path = _safe_audio_path(audio_path)
    sample_name = _clean_sample_name(sample_name)
    message = _app().save_prep_sample(
        audio_path=audio_path,
        sample_name=sample_name,
        transcription=transcription,
    )
    message_text = str(message)
    if not message_text or "error" in message_text.lower():
        raise RuntimeError(message or "save_prep_sample failed")
    return sample_name


@mcp.tool()
def delete_sample(sample_name: str) -> str:
    """Delete a saved voice sample by name."""
    sample_name = _clean_sample_name(sample_name)
    message = _app().delete_sample(sample_name=sample_name)
    if not message or "Error" in str(message):
        raise RuntimeError(message or "delete_sample failed")
    json_path = os.path.join(SAMPLES_DIR, f"{sample_name}.json")
    if os.path.exists(json_path):
        os.remove(json_path)
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
        sample = _clean_sample_name(seg.get("sample") or "")
        text = (seg.get("text") or "").strip()
        if not text:
            raise ValueError(f"segments[{i}].text missing")
        if not os.path.isfile(_sample_audio_path(sample)):
            raise FileNotFoundError(f"segments[{i}].sample not found: {sample!r}")
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


def _build_http_app(transport: str, username: str, password: str):
    if transport == "sse":
        app = mcp.sse_app()
    else:
        app = mcp.streamable_http_app()
    app.add_middleware(BasicAuthMiddleware, username=username, password=password)
    return app


def _run_http(transport: str, host: str, port: int, username: str, password: str):
    import uvicorn

    app = _build_http_app(transport, username, password)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _allow_public_hosts():
    public_hosts = [
        item.strip()
        for item in os.environ.get("FISH_MCP_PUBLIC_HOSTS", "mcp.thethirdroom.xyz").split(",")
        if item.strip()
    ]
    security = mcp.settings.transport_security
    if not security or not public_hosts:
        return

    allowed_hosts = set(security.allowed_hosts)
    allowed_origins = set(security.allowed_origins)
    for host in public_hosts:
        allowed_hosts.add(host)
        allowed_hosts.add(f"{host}:443")
        allowed_origins.add(f"https://{host}")

    security.allowed_hosts = sorted(allowed_hosts)
    security.allowed_origins = sorted(allowed_origins)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Fish Speech S2 Pro MCP server")
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"),
                        default=os.environ.get("FISH_MCP_TRANSPORT", "streamable-http"))
    parser.add_argument("--host", default=os.environ.get("FISH_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("FISH_MCP_PORT", "8765")))
    parser.add_argument("--username", default=os.environ.get("FISH_MCP_USERNAME", "fish"))
    parser.add_argument("--password-env", default="FISH_MCP_PASSWORD",
                        help="Environment variable containing the HTTP Basic auth password.")
    parser.add_argument("--allow-no-auth", action="store_true",
                        help="Allow HTTP/SSE without a password. Only use on trusted local networks.")
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
        _allow_public_hosts()
        password = os.environ.get(args.password_env, "")
        if not password and not args.allow_no_auth:
            sys.stderr.write(f"Set {args.password_env} before exposing HTTP MCP, or pass --allow-no-auth.\n")
            sys.exit(2)
        if password:
            _run_http(args.transport, args.host, args.port, args.username, password)
        else:
            mcp.run(transport=args.transport)
