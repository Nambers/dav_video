"""mpv controller over the JSON IPC socket.

mpv runs as a persistent, idle, force-window process driven through a unix
socket (``--input-ipc-server``), so all rendering, seeking, shader and codec
concerns stay inside mpv and the user's ~/.config/mpv keeps working.

Render flags (core/render.py) are appended to the launch argv. They are
launch-time only -- ``vo``/``gpu-api`` cannot change on a live mpv -- so
``set_render_args`` stops the player and the next play relaunches it.

stdlib only.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

# How long to wait for a reply to one IPC command. mpv answers commands
# immediately (``loadfile`` returns before the file is open), so anything past
# this means mpv is wedged and the caller should get an error instead of a
# worker thread parked on recv() forever.
IPC_TIMEOUT = 10.0


class MpvError(Exception):
    pass


def default_socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, f"dav_video-mpv-{os.getpid()}.sock")


class MpvController:
    def __init__(self, socket_path: str | None = None, extra_args: list[str] | None = None):
        self.socket_path = socket_path or default_socket_path()
        self._extra_args = extra_args or []
        self._render_args: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._buf = b""
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def render_args(self) -> list[str]:
        return list(self._render_args)

    def set_render_args(self, args: list[str]) -> bool:
        """Swap the render flags. Returns True if a running mpv had to be
        stopped to apply them (the next play relaunches it)."""
        args = list(args)
        if args == self._render_args:
            return False
        self._render_args = args
        if self.running:
            self.quit()
            return True
        return False

    def ensure_started(self) -> None:
        if self.running and self._sock is not None:
            return
        if shutil.which("mpv") is None:
            raise MpvError("mpv not found on PATH -- install mpv first")
        self._unlink_socket()
        self._proc = subprocess.Popen(
            [
                "mpv",
                "--idle=yes",
                "--force-window=yes",
                "--keep-open=yes",
                f"--input-ipc-server={self.socket_path}",
                # render flags first, then ctor extras, so a caller-supplied
                # arg can still override them (later mpv flags win).
                *self._render_args,
                *self._extra_args,
            ]
        )
        try:
            self._connect()
        except MpvError:
            # We spawned it, so we own it: an mpv we can't talk to must not be
            # left running, or every retry orphans another window.
            proc, self._proc = self._proc, None
            if proc is not None:
                self._reap(proc, 0.5)
            self._unlink_socket()
            raise

    def _connect(self) -> None:
        for _ in range(100):  # up to ~10s
            if os.path.exists(self.socket_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.socket_path)
                    s.settimeout(IPC_TIMEOUT)
                    self._sock = s
                    self._buf = b""
                    return
                except OSError:
                    pass
            if not self.running:
                raise MpvError("mpv exited before IPC socket was ready")
            time.sleep(0.1)
        raise MpvError("timed out waiting for mpv IPC socket")

    def _unlink_socket(self) -> None:
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def quit(self, timeout: float = 3.0) -> None:
        """Stop mpv and release everything. Safe to call twice, or when nothing
        was ever started."""
        proc, sock = self._proc, self._sock
        self._proc, self._sock, self._buf = None, None, b""
        if sock is not None:
            # mpv only writes a watch-later file on quit with
            # --save-position-on-quit (default: no), so ask for it explicitly --
            # otherwise the wait below buys nothing and resume is lost.
            acquired = self._lock.acquire(timeout=1.0)
            try:
                for command in (["write-watch-later-config"], ["quit"]):
                    try:
                        sock.sendall((json.dumps({"command": command}) + "\n").encode())
                    except OSError:
                        break
            finally:
                if acquired:
                    self._lock.release()
            try:
                sock.close()
            except OSError:
                pass
        if proc is not None:
            self._reap(proc, timeout)
        self._unlink_socket()

    @staticmethod
    def _reap(proc: subprocess.Popen, timeout: float) -> None:
        """Wait for mpv, escalating if it ignores us. Every branch ends in a
        wait() so we never leave a zombie behind."""
        for stage, grace in ((None, timeout), (proc.terminate, 2.0), (proc.kill, 2.0)):
            if stage is not None:
                try:
                    stage()
                except OSError:
                    return
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    # -- low-level IPC ----------------------------------------------------
    def _readline(self) -> bytes:
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(65536)  # type: ignore[union-attr]
            except socket.timeout as exc:
                raise MpvError(f"mpv did not answer within {IPC_TIMEOUT:g}s") from exc
            if not chunk:
                raise MpvError("mpv closed the IPC connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def command(self, *args, expect_reply: bool = True):
        """Send one command. Returns mpv's reply dict (or None if no reply
        requested). Async events are skipped while waiting for our reply."""
        if self._sock is None:
            raise MpvError("not connected to mpv")
        req_id = next(self._ids)
        payload = json.dumps({"command": list(args), "request_id": req_id}) + "\n"
        with self._lock:
            self._sock.sendall(payload.encode())
            if not expect_reply:
                return None
            while True:
                try:
                    data = json.loads(self._readline())
                except json.JSONDecodeError:
                    continue
                if data.get("request_id") == req_id:
                    if data.get("error") not in (None, "success"):
                        raise MpvError(f"mpv error: {data.get('error')} for {args}")
                    return data

    def set_property(self, name: str, value) -> None:
        self.command("set_property", name, value)

    def get_property(self, name: str):
        return self.command("get_property", name).get("data")

    # -- high level -------------------------------------------------------
    def _apply_auth(self, auth_header: str | None) -> None:
        # Always assign: http-header-fields is global and sticky, so an
        # unauthenticated URL played after an authenticated one would otherwise
        # still be sent the previous server's credentials.
        self.set_property("http-header-fields", [auth_header] if auth_header else [])

    def _save_resume_point(self) -> None:
        """Persist the currently-playing file's position before we load another,
        so resume-playback restores it next time. Harmless no-op when nothing is
        loaded yet (e.g. the very first play)."""
        try:
            self.command("write-watch-later-config")
        except MpvError:
            pass

    def play_url(self, url: str, auth_header: str | None = None,
                 subtitles: list[str] | None = None) -> int:
        """Load ``url`` and best-effort sideload ``subtitles``. Returns the count
        of subtitles that actually loaded. A subtitle that mpv can't open must
        never abort playback of the video itself, so sub-add errors are swallowed
        (they're heuristic siblings, not something the user explicitly chose)."""
        self.ensure_started()
        self._apply_auth(auth_header)
        self._save_resume_point()          # remember where the previous file was
        self.command("loadfile", url, "replace")
        loaded = 0
        for sub in subtitles or []:
            try:
                self.command("sub-add", sub, "select" if loaded == 0 else "auto")
                loaded += 1
            except MpvError:
                pass
        return loaded

    def play_playlist(self, items: list[tuple[str, str | None, str | None]]) -> int:
        """items: (url, auth_header, subtitle_url). Returns how many entries got
        a subtitle attached.

        Each entry carries its OWN sidecar subtitle, so episode 2 gets episode
        2's subtitles when mpv walks on to it -- there is nothing to press.

        Exactly one subtitle per entry, and that is mpv's limit, not a choice:
        ``sub-files`` is a ``:``-separated path list, so an http:// URL gets cut
        at its own scheme; ``sub-files-append`` takes a single value without
        splitting, and repeating it in one option string keeps only the last.
        (Measured on mpv 0.41. ``sub-add`` has no such limit but only applies to
        the file already loaded -- which is what ``play_url`` uses.)

        Note: mpv's http-header-fields is global, so a mixed-server queue uses
        the first item's auth. Fine for the common single-server case.
        """
        self.ensure_started()
        if not items:
            return 0
        self._apply_auth(items[0][1])
        self._save_resume_point()          # remember where the previous file was
        attached = 0
        for index, (url, _auth, subtitle) in enumerate(items):
            flag = "replace" if index == 0 else "append"
            if subtitle:
                try:
                    self.command("loadfile", url, flag, 0,
                                 {"sub-files-append": subtitle})
                    attached += 1
                    continue
                except MpvError:
                    # Per-file options need a recent mpv; an older one must
                    # still play the video, just without the sidecar.
                    pass
            self.command("loadfile", url, flag)
        return attached

    def track_list(self) -> list[dict]:
        return self.get_property("track-list") or []

    def subtitle_languages(self) -> list[str]:
        """mpv's own ``--slang`` preference, in order.

        Read rather than configured: the user already told mpv which subtitle
        language they want, in ~/.config/mpv. Asking mpv beats inventing a
        second setting that would then disagree with it. Empty when unset or
        unreadable, which just means "no preference".
        """
        try:
            return [str(x) for x in (self.get_property("slang") or [])]
        except Exception:  # noqa: BLE001 - a preference is never worth an error
            return []

