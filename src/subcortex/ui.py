"""Terminal prompts for interactive setup (stdlib only).

In a real terminal, menus use the arrow keys (↑/↓ or j/k to move, space to
toggle, a to toggle all, enter to accept, esc/q to cancel). Anywhere else —
pipes, CI, tests — every prompt falls back to plain numbered line input, and
with no input at all each prompt returns its default. ``Cancelled`` is raised
when the user backs out (esc, q, ctrl-c).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Sequence, Set, TextIO, Tuple

Option = Tuple[Any, str, str]  # (value, label, hint)

UP, DOWN, SPACE, ENTER, CANCEL, TOGGLE_ALL = "up", "down", "space", "enter", "cancel", "all"


class Cancelled(Exception):
    pass


def _read_key(stream: TextIO) -> str:
    """One keypress from a terminal in cbreak mode, normalized."""
    ch = stream.read(1)
    if ch == "\x1b":
        nxt = stream.read(1)
        if nxt == "[":
            code = stream.read(1)
            return {"A": UP, "B": DOWN}.get(code, "")
        return CANCEL
    if ch in ("\r", "\n"):
        return ENTER
    if ch == " ":
        return SPACE
    if ch in ("k",):
        return UP
    if ch in ("j",):
        return DOWN
    if ch in ("a", "A"):
        return TOGGLE_ALL
    if ch in ("q", "Q", "\x03", "\x04"):
        return CANCEL
    return ""


class UI:
    def __init__(self, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None,
                 interactive: Optional[bool] = None, color: Optional[bool] = None,
                 keys: Optional[Iterator[str]] = None) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        tty = _isatty(self.stdin) and _isatty(self.stdout) and os.environ.get("TERM") != "dumb"
        self.interactive = tty if interactive is None else interactive
        self.color = (tty and not os.environ.get("NO_COLOR")) if color is None else color
        self._keys = keys  # injected keypresses (tests)

    # -- output -------------------------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def write(self, text: str = "") -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def title(self, text: str) -> None:
        self.write()
        self.write(self._c("1", f"◆ {text}"))

    def step(self, n: int, total: int, text: str) -> None:
        self.write()
        self.write(self._c("1;36", f"[{n}/{total}] {text}"))

    def info(self, text: str) -> None:
        self.write(f"  {text}")

    def dim(self, text: str) -> None:
        self.write(self._c("2", f"  {text}"))

    def ok(self, text: str) -> None:
        self.write(f"  {self._c('32', '✓')} {text}")

    def warn(self, text: str) -> None:
        self.write(f"  {self._c('33', '!')} {text}")

    def error(self, text: str) -> None:
        self.write(f"  {self._c('31', '✗')} {text}")

    # -- line input -----------------------------------------------------------------------

    def _readline(self, prompt: str) -> Optional[str]:
        self.stdout.write(prompt)
        self.stdout.flush()
        try:
            line = self.stdin.readline()
        except KeyboardInterrupt:
            raise Cancelled()
        if line == "":  # EOF: no more input, use defaults
            self.stdout.write("\n")
            return None
        return line.rstrip("\n")

    def confirm(self, question: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self._readline(f"  {question} {self._c('2', suffix)} ")
            if answer is None or not answer.strip():
                return default
            if answer.strip().lower() in ("y", "yes"):
                return True
            if answer.strip().lower() in ("n", "no"):
                return False
            self.warn("please answer y or n")

    def ask(self, question: str, default: str = "", secret: bool = False,
            validate: Optional[Callable[[str], Optional[str]]] = None) -> str:
        shown = f" {self._c('2', f'[{default}]')}" if default and not secret else ""
        while True:
            if secret and self.interactive:
                import getpass

                try:
                    answer: Optional[str] = getpass.getpass(f"  {question}: ", stream=self.stdout)
                except (EOFError, KeyboardInterrupt):
                    raise Cancelled()
            else:
                answer = self._readline(f"  {question}{shown}: ")
            value = default if answer is None or not answer.strip() else answer.strip()
            problem = validate(value) if validate else None
            if not problem:
                return value
            self.warn(problem)
            if answer is None:  # no more input: don't loop forever
                raise Cancelled()

    # -- menus ------------------------------------------------------------------------------

    def choose(self, question: str, options: Sequence[Option], default: int = 0) -> Any:
        """Single choice; returns the chosen option's value."""
        if not options:
            raise ValueError("no options")
        if self.interactive:
            index = self._menu(question, options, cursor=default, multi=False, selected=set())
            return options[index][0]
        self.write(f"  {question}")
        for i, (_, label, hint) in enumerate(options, 1):
            marker = "*" if i - 1 == default else " "
            self.write(f"   {marker}{i}) {label}" + (f" — {hint}" if hint else ""))
        while True:
            answer = self._readline(f"  choice {self._c('2', f'[{default + 1}]')}: ")
            if answer is None or not answer.strip():
                return options[default][0]
            if answer.strip().isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            self.warn(f"enter a number from 1 to {len(options)}")

    def checklist(self, question: str, options: Sequence[Option], selected: Set[Any]) -> List[Any]:
        """Multiple choice; returns the chosen values in option order."""
        chosen = {i for i, (value, _, _) in enumerate(options) if value in selected}
        if self.interactive:
            self._menu(question, options, cursor=0, multi=True, selected=chosen)
            return [options[i][0] for i in sorted(chosen)]
        while True:
            self.write(f"  {question}")
            for i, (_, label, hint) in enumerate(options, 1):
                box = "x" if i - 1 in chosen else " "
                self.write(f"   [{box}] {i:>2}) {label}" + (f" — {hint}" if hint else ""))
            answer = self._readline("  numbers to toggle (e.g. 1 3), 'all', 'none', or enter to accept: ")
            if answer is None or not answer.strip():
                return [options[i][0] for i in sorted(chosen)]
            tokens = answer.replace(",", " ").split()
            if tokens == ["all"]:
                chosen = set(range(len(options)))
            elif tokens == ["none"]:
                chosen = set()
            elif all(t.isdigit() and 1 <= int(t) <= len(options) for t in tokens):
                chosen ^= {int(t) - 1 for t in tokens}
            else:
                self.warn(f"use numbers from 1 to {len(options)}")

    def _next_key(self) -> str:
        if self._keys is not None:
            try:
                return next(self._keys)
            except StopIteration:
                return ENTER
        return _read_key(self.stdin)

    def _menu(self, question: str, options: Sequence[Option], cursor: int, multi: bool,
              selected: Set[int]) -> int:
        help_text = ("↑/↓ move · space toggle · a all · enter accept · esc cancel" if multi
                     else "↑/↓ move · enter select · esc cancel")
        visible = min(len(options), self._max_visible())
        scrolling = visible < len(options)
        lines = visible + (2 if scrolling else 0)  # constant height keeps redraws aligned
        top = 0
        # cbreak before the question is shown: keys typed as soon as it appears
        # must reach the menu, not get echoed and line-buffered by the terminal.
        with self._raw_mode():
            self.write(f"  {question}")
            self.dim(help_text)
            self.stdout.write("\x1b[?25l")  # hide cursor
            try:
                first = True
                while True:
                    if not first:
                        self.stdout.write(f"\x1b[{lines}A")
                    first = False
                    top = min(max(top, cursor - visible + 1), cursor)  # keep the cursor in view
                    window = range(top, top + visible)
                    if scrolling:
                        more = f"↑ {top} more" if top else ""
                        self.stdout.write(f"\x1b[2K    {self._c('2', more)}\n")
                    for i in window:
                        _, label, hint = options[i]
                        pointer = self._c("36", "❯") if i == cursor else " "
                        box = ""
                        if multi:
                            box = (self._c("32", "◉") if i in selected else "○") + " "
                        text = f"{label}" + (self._c("2", f"  {hint}") if hint else "")
                        if i == cursor:
                            text = self._c("1", label) + (self._c("2", f"  {hint}") if hint else "")
                        self.stdout.write(f"\x1b[2K  {pointer} {box}{text}\n")
                    if scrolling:
                        below = len(options) - top - visible
                        more = f"↓ {below} more" if below else ""
                        self.stdout.write(f"\x1b[2K    {self._c('2', more)}\n")
                    self.stdout.flush()
                    key = self._next_key()
                    if key == UP:
                        cursor = (cursor - 1) % len(options)
                    elif key == DOWN:
                        cursor = (cursor + 1) % len(options)
                    elif key == SPACE and multi:
                        selected ^= {cursor}
                    elif key == TOGGLE_ALL and multi:
                        if len(selected) == len(options):
                            selected.clear()
                        else:
                            selected.update(range(len(options)))
                    elif key == ENTER:
                        return cursor
                    elif key == CANCEL:
                        raise Cancelled()
            finally:
                self.stdout.write("\x1b[?25h")
                self.stdout.flush()

    def _max_visible(self) -> int:
        """Menu rows that fit the terminal (header, help and margins excluded)."""
        import shutil

        rows = shutil.get_terminal_size(fallback=(80, 24)).lines
        return max(5, rows - 8)

    @contextmanager
    def _raw_mode(self) -> Iterator[None]:
        if self._keys is not None:
            yield
            return
        try:
            import termios
            import tty

            fd = self.stdin.fileno()
            saved = termios.tcgetattr(fd)
        except Exception:
            yield
            return
        try:
            # TCSADRAIN, not setcbreak's default TCSAFLUSH: that discards type-ahead.
            tty.setcbreak(fd, termios.TCSADRAIN)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    # -- progress ---------------------------------------------------------------------------

    @contextmanager
    def spinner(self, text: str) -> Iterator[None]:
        """Show elapsed time next to ``text`` while a slow step runs."""
        if not self.interactive:
            self.info(f"{text}…")
            yield
            return
        done = threading.Event()
        started = time.time()

        def spin() -> None:
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i = 0
            while not done.wait(0.1):
                self.stdout.write(f"\r\x1b[2K  {frames[i % len(frames)]} {text} "
                                  f"{self._c('2', f'{time.time() - started:.0f}s')}")
                self.stdout.flush()
                i += 1

        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()
            self.stdout.write("\r\x1b[2K")
            self.stdout.flush()


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False
