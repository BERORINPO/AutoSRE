"""AutoSRE watch - the ledger, the approval queue and the guard on one screen.

The third thing the judges asked for, after a CLI and a Quickstart: until now
the only way to read the trust ledger was `autosre status` (one shot, scrolls
away) or the web console (which needs the whole system deployed first). Neither
answers the question an operator actually holds open on a second monitor: what
has this agent earned the right to do, and what is waiting for me right now?

Three deliberate constraints:

  * **Stdlib only, at import time and at run time.** A terminal UI that needs
    `pip install textual` is a dependency the operator has to accept before
    they can look at the ledger - and on Windows even `curses` is a third-party
    wheel. So this is plain ANSI: alternate screen, full redraw, VT enabled
    explicitly on Windows, and a documented fallback to scrolling frames when
    the terminal cannot do ANSI at all.
  * **The renderer is pure.** `render()` takes a snapshot dict and returns
    lines; nothing in it reads the network, the clock or the terminal. That is
    what the offline gate drives, and it is why a half-broken snapshot (an
    unreadable ledger, a GitHub outage) draws a panel that says so instead of
    taking the screen down with a traceback.
  * **The TUI is never required.** Every panel here is a view over data the CLI
    already prints - `autosre status --json` for the guard, the ledger and the
    target, `autosre watch --once --json` for the whole snapshot including the
    queue - and the collectors are injected by the CLI rather than reached for,
    so the screen can never grow a capability the scriptable path does not have.

ASCII only on purpose, same reason as `cli.format_step`: this runs in cmd.exe
and PowerShell as often as in a UTF-8 terminal, and a UnicodeEncodeError in a
redraw loop would kill the one window an operator was watching.
"""
import datetime
import json
import os
import sys
import time
import webbrowser

# Redraw cadence. 10s is slow enough that a BigQuery ledger read per tick stays
# free-tier noise, fast enough that "PR opened" shows up while you are looking.
DEFAULT_INTERVAL_S = 10.0
MIN_INTERVAL_S = 2.0

MIN_WIDTH = 60
DEFAULT_WIDTH = 100
DEFAULT_HEIGHT = 40

# How many PRs get an open-me hotkey. 1-9 only: two-digit key handling would
# need a submit key, and a queue that deep is a different problem than this
# screen solves.
MAX_HOTKEY_PRS = 9


# --------------------------------------------------------------------------
# snapshot (collectors are injected - see cli._watch_collectors)
# --------------------------------------------------------------------------
def build_snapshot(collectors: dict, now: float | None = None) -> dict:
    """Call every collector, isolating each failure to its own panel.

    A snapshot is a status report, so one dead source must degrade one panel:
    if the GitHub token expired, the trust ledger is still worth looking at.
    Failures are recorded as an `error` string on the section - never swallowed,
    never raised - and the renderer prints them where the panel would have been.
    """
    snapshot = {"generated_at": time.time() if now is None else now, "sections": {}}
    for name, fetch in collectors.items():
        try:
            value = fetch()
        # SystemExit is explicit: the CLI raises it for expected, explained
        # failures (a --remote call with no console key). On a one-shot verb
        # that is the right answer; on a screen, taking every panel down
        # because one of them is unauthorized is not.
        except (Exception, SystemExit) as e:  # noqa: BLE001 - a panel outage is not a crash
            value = {"error": f"{type(e).__name__}: {e}"}
        snapshot["sections"][name] = value if isinstance(value, dict) else {"value": value}
    return snapshot


def section(snapshot: dict, name: str) -> dict:
    value = (snapshot.get("sections") or {}).get(name)
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# pure formatting helpers
# --------------------------------------------------------------------------
def _ascii(text) -> str:
    """Make one LINE safe to print: no control characters, no non-ASCII.

    Runs of spaces survive on purpose - this is applied last, to lines whose
    padding is already computed, and collapsing whitespace here would flatten
    every column the panels just aligned.
    """
    rendered = str(text).replace("\t", "    ")
    rendered = "".join(ch if ch.isprintable() else " " for ch in rendered)
    return rendered.encode("ascii", "replace").decode("ascii")


def clip(text, width: int) -> str:
    """Fit one assembled line to the terminal width, keeping its spacing."""
    text = _ascii(text)
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


def field(text, width: int) -> str:
    """Fit one VALUE into a column: collapse its whitespace first.

    A PR title or a root cause arrives as free text and may carry newlines; a
    single embedded newline would otherwise split one row into two and leave
    the frame one line too tall.
    """
    return clip(" ".join(str(text).split()), width)


def wilson_lower_bound(successes: int, attempts: int) -> float:
    """Re-exported from agents.autonomy, whose decision core is stdlib-only."""
    from agents.autonomy import wilson_lower_bound as wlb

    return wlb(successes, attempts)


def runs_to_promote(successes: int, attempts: int, threshold: float,
                    cap: int = 200) -> int | None:
    """How many more consecutive verified recoveries would clear the bar.

    This is the number the ledger cannot show on its own: a class sitting at
    lb=0.44 tells you it is not promoted, not how far away it is. Answering
    "3 more" turns the gate's retirement schedule into something an operator
    can plan around. None means "not within `cap` more runs".
    """
    if attempts < 0 or successes < 0:
        return None
    for extra in range(0, cap + 1):
        if wilson_lower_bound(successes + extra, attempts + extra) >= threshold:
            return extra
    return None


def bar(value: float, threshold: float, width: int = 14) -> str:
    """`[####|###  ]` - filled to `value`, with the promotion bar drawn in place.

    The marker is written last and always wins its cell: a threshold you cannot
    see because the fill covered it defeats the point of drawing the bar.
    """
    width = max(4, int(width))
    value = min(max(float(value), 0.0), 1.0)
    threshold = min(max(float(threshold), 0.0), 1.0)
    cells = ["#" if i < round(value * width) else " " for i in range(width)]
    marker = min(width - 1, int(threshold * width))
    cells[marker] = "|"
    return "[" + "".join(cells) + "]"


def _fmt_ts(value, fmt: str = "%m-%d %H:%M") -> str:
    """Local-time stamp from an ISO string or an epoch float. '-' when unreadable."""
    try:
        if isinstance(value, (int, float)) and value > 0:
            return datetime.datetime.fromtimestamp(value).strftime(fmt)
        if isinstance(value, str) and value.strip():
            parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone()
            return parsed.strftime(fmt)
    except (ValueError, OSError, OverflowError):
        pass
    return "-"


def _fmt_age(value, now: float) -> str:
    """Coarse 'opened 2h ago' age. Coarse on purpose: the queue is a queue."""
    try:
        if isinstance(value, str) and value.strip():
            ts = datetime.datetime.fromisoformat(
                value.strip().replace("Z", "+00:00")).timestamp()
        elif isinstance(value, (int, float)):
            ts = float(value)
        else:
            return "-"
    except (ValueError, OSError, OverflowError):
        return "-"
    delta = max(0.0, now - ts)
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta // 60)}m ago"
    if delta < 172800:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _row(left: str, right: str, width: int) -> str:
    """One line with `right` pushed to the margin, degrading to a two-space gap."""
    left, right = _ascii(left), _ascii(right)
    gap = width - len(left) - len(right)
    return clip(left + (" " * gap + right if gap >= 2 else "  " + right), width)


def _panel(title: str, right: str, width: int) -> list[str]:
    """Title row with a right-aligned annotation, then a rule."""
    head = _row(title, right, width)
    return [head, "-" * min(width, max(MIN_WIDTH, len(head)))]


def _unavailable(payload: dict, disabled_note: str, *, enabled_means_configured: bool = True,
                 off_reason: str = "") -> str | None:
    """One line explaining why a panel has no rows, or None when it has data.

    "Off", "unreadable" and "empty" are three different states and the screen
    has to keep them apart - a disabled case store rendering as an empty ledger
    is the same class of lie as a green console over a 503.

    `enabled` does NOT mean the same thing on every payload, which is exactly
    how this helper told that lie once already. On the cost guard and the case
    store it means "this source is configured" (state_store.read_state,
    case_store.recall_similar_cases). On the trust ledger it is the
    AUTOSRE_AUTONOMY_ENABLED arming flag, and the ledger is deliberately
    readable while autonomy is off - so that caller passes
    enabled_means_configured=False and names the `reason` string that really
    does mean off.
    """
    if payload.get("error"):
        return f"unavailable: {field(payload['error'], 160)}"
    if enabled_means_configured and payload.get("enabled") is False:
        return disabled_note
    if payload.get("available") is False:
        reason = payload.get("reason") or "source did not answer"
        if off_reason and reason == off_reason:
            return disabled_note
        return f"unreadable: {field(reason, 140)}"
    if payload.get("ok") is False:
        return f"unavailable: {field(payload.get('error') or 'source did not answer', 140)}"
    return None


# --------------------------------------------------------------------------
# panels
# --------------------------------------------------------------------------
def header_lines(snapshot: dict, meta: dict, width: int) -> list[str]:
    now = snapshot.get("generated_at") or 0
    health = section(snapshot, "target_health")
    if health.get("ok"):
        state = (f"target HTTP {health.get('status_code')}"
                 + (" (healthy)" if health.get("healthy") else " (down)"))
    else:
        state = "target health unknown"
    line = _row(
        f"AutoSRE watch  {meta.get('service') or '-'}  via {field(meta.get('remote') or 'local', 44)}",
        f"{state}   {_fmt_ts(now, '%H:%M:%S')}",
        width,
    )
    return [line, "=" * min(width, max(MIN_WIDTH, len(line)))]


def guard_lines(snapshot: dict, width: int) -> list[str]:
    guard = section(snapshot, "guard")
    lines = _panel("cost guard", "", width)
    note = _unavailable(guard, "off (AUTOSRE_STATE_URI unset - runs are not budgeted)")
    if note:
        return lines + [clip("  " + note, width), ""]
    limit = guard.get("daily_limit")
    used = guard.get("runs_today", 0)
    kill = guard.get("killswitch") or {}
    verdict = (f"TRIPPED - {field(kill.get('reason') or 'no reason recorded', 60)}"
               if kill.get("tripped") else "clear")
    lines.append(clip(
        f"  runs today {f'{used}/{limit}' if limit else used} (day {guard.get('day') or '-'})"
        f"   kill switch {verdict}", width))
    if guard.get("last_run_ts"):
        lines.append(clip(f"  last run   {_fmt_ts(guard['last_run_ts'], '%m-%d %H:%M:%S')}",
                          width))
    lines.append("")
    return lines


def ledger_lines(snapshot: dict, width: int) -> list[str]:
    """The trust ledger: evidence, the bar, and the distance left to promotion."""
    book = section(snapshot, "ledger")
    thr = book.get("threshold")
    # Only claim an arming state when the payload actually carries one: a failed
    # read has no `enabled` key, and "autonomy off" would then be an assertion
    # made from no evidence, printed directly above "I could not read this".
    if "enabled" in book:
        armed = "autonomy ARMED" if book.get("enabled") else "autonomy off (reporting only)"
    else:
        armed = "arming unknown"
    lines = _panel(
        "trust ledger - what may act without a click",
        f"threshold {thr:.2f}   {armed}" if isinstance(thr, (int, float)) else armed,
        width,
    )
    # `enabled` here is AUTOSRE_AUTONOMY_ENABLED, not "the case store is on" -
    # autonomy is off by default and the ledger is still readable, so keying the
    # panel off it hid a populated ledger on every normal install. The ledger's
    # own word for "the store is off" is available=False + this reason string.
    note = _unavailable(book, "off (AUTOSRE_CASES_TABLE unset - no verified record kept)",
                        enabled_means_configured=False, off_reason="case memory disabled")
    if note:
        return lines + [clip("  " + note, width), ""]
    classes = book.get("classes") or []
    if not classes:
        return lines + [
            "  no verified recoveries recorded yet - every class stays behind the gate",
            "",
        ]
    thr = thr if isinstance(thr, (int, float)) else 0.80
    key_w = min(30, max(len(_ascii(c.get("class") or "?")) for c in classes))
    # Columns are padded to the widest row so the bars line up: a ledger read
    # at a glance is the whole point of putting it on a screen.
    ev_w = max(len(f"{c.get('successes')}/{c.get('attempts')}") for c in classes)
    for cls in classes:
        successes = int(cls.get("successes") or 0)
        attempts = int(cls.get("attempts") or 0)
        lb = cls.get("wilson_lower_bound")
        lb = float(lb) if isinstance(lb, (int, float)) else wilson_lower_bound(successes, attempts)
        if cls.get("demoted"):
            demotion = cls.get("demotion") or {}
            verdict = "DEMOTED " + _fmt_ts(demotion.get("ts"))
            tail = field(demotion.get("reason") or "human re-arm needed", 40)
        elif cls.get("promoted"):
            verdict, tail = "PROMOTED", "acts unattended"
        else:
            more = runs_to_promote(successes, attempts, thr)
            verdict = "gated"
            tail = f"{more} more to promote" if more else "far from the bar"
        lines.append(clip(
            f"  {field(cls.get('class') or '?', key_w).ljust(key_w)}  "
            f"{f'{successes}/{attempts}'.rjust(ev_w)} verified  {bar(lb, thr)} "
            f"lb {lb:.2f}  {verdict} - {tail}",
            width))
    lines.append("")
    return lines


def approvals_lines(snapshot: dict, width: int, limit: int = MAX_HOTKEY_PRS) -> list[str]:
    """The queue: fix PRs the agent opened and a human has not answered yet."""
    payload = section(snapshot, "pull_requests")
    lines = _panel("awaiting your approval", "press 1-9 to open in a browser", width)
    note = _unavailable(payload, "off (no GitHub token configured on this machine)")
    if note:
        return lines + [clip("  " + note, width), ""]
    prs = payload.get("pull_requests") or []
    if not prs:
        return lines + ["  queue empty - no open AutoSRE fix PR is waiting", ""]
    now = snapshot.get("generated_at") or time.time()
    for i, pr in enumerate(prs[:limit], 1):
        number = pr.get("number")
        lines.append(clip(
            f"  {i}  #{number}  {pr.get('title') or '(no title)'}"
            f"   opened {_fmt_age(pr.get('created_at'), now)}", width))
        lines.append(clip(f"        {pr.get('url') or '-'}", width))
        if number is not None:
            lines.append(clip(f"        autosre approve {number}", width))
    if len(prs) > limit:
        lines.append(clip(f"  (+{len(prs) - limit} more not shown)", width))
    lines.append("")
    return lines


def cases_lines(snapshot: dict, width: int, limit: int = 5) -> list[str]:
    """Past diagnoses and how they ended - the rows the ledger is computed from."""
    payload = section(snapshot, "cases")
    lines = _panel("recent incidents", "newest first", width)
    note = _unavailable(payload, "off (AUTOSRE_CASES_TABLE unset - case memory disabled)")
    if note:
        return lines + [clip("  " + note, width), ""]
    cases = payload.get("cases") or []
    if not cases:
        return lines + ["  no incidents recorded for this service yet", ""]
    outcomes = {
        "verified_recovered": "recovered (verified)",
        "recovery_failed": "RECOVERY FAILED",
        "unresolved_or_pending": "awaiting approval",
    }
    for case in cases[:limit]:
        confidence = case.get("confidence")
        conf = f"conf {confidence:.2f}" if isinstance(confidence, (int, float)) else "conf -"
        pr = f"#{case['pr_number']}" if case.get("pr_number") else "no PR"
        outcome = outcomes.get(case.get("outcome"), case.get("outcome") or "-")
        lines.append(clip(
            f"  {_fmt_ts(case.get('when'))}  {case.get('missing_env_var') or '-'}  "
            f"{pr}  {conf}  {outcome}", width))
        if case.get("root_cause"):
            lines.append(clip(f"        {case['root_cause']}", width))
    lines.append("")
    return lines


def footer_lines(meta: dict, width: int) -> list[str]:
    if meta.get("static"):
        keys = "  (one frame; run without --once for a live screen)"
    else:
        interval = meta.get("interval") or DEFAULT_INTERVAL_S
        keys = ("  r reload now   1-9 open a PR   q quit"
                f"          refreshing every {interval:.0f}s")
    return ["-" * width, clip(keys, width)]


def render(snapshot: dict, meta: dict | None = None, width: int = DEFAULT_WIDTH,
           height: int = DEFAULT_HEIGHT) -> list[str]:
    """The whole frame. Pure: no I/O, no clock, no terminal - this is the gate.

    Panels are ordered by what an operator acts on: the queue that needs a
    decision, then the ledger that says which decisions are still theirs to
    make, then the evidence, then the budget. When the terminal is too short
    the evidence is what gets trimmed - never the queue.
    """
    meta = meta or {}
    width = max(MIN_WIDTH, int(width))
    body = header_lines(snapshot, meta, width) + [""]
    body += approvals_lines(snapshot, width)
    body += ledger_lines(snapshot, width)
    footer = footer_lines(meta, width)

    room = int(height) - len(body) - len(footer) - 1
    cases = cases_lines(snapshot, width)
    guard = guard_lines(snapshot, width)
    if room >= len(cases) + len(guard):
        body += cases + guard
    elif room >= len(guard) + 4:
        body += cases[: room - len(guard) - 1]
        body += [clip("  (screen too short for the rest - see `autosre status`)", width)]
        body += guard
    elif room >= len(guard):
        body += guard
    elif room >= 1:
        body += [clip("  (screen too short - `autosre status` prints the rest)", width)]
    # rstrip last: panels right-align by padding, and a frame full of trailing
    # spaces wraps on a narrow terminal for no visible reason.
    return [line.rstrip() for line in body + footer]


# --------------------------------------------------------------------------
# terminal (impure - kept away from render on purpose)
# --------------------------------------------------------------------------
def _enable_vt() -> bool:
    """Turn on ANSI on a Windows console. False = fall back to scrolling frames."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - no ANSI is a degraded screen, not a failure
        return False


class Screen:
    """Alternate-screen redraw + non-blocking keys, with honest degradation.

    Two fallbacks matter and both keep the screen useful: a console that cannot
    do ANSI prints successive frames (scrolling, still auto-refreshing), and a
    stdin that is not a tty sleeps the interval out instead of blocking on a key
    read - so a redirected stdin still refreshes, it just cannot be steered.
    (A non-tty *stdout* never reaches this class at all: `watch` prints one
    frame and returns, which is what a pipe or a CI log wants.)
    """

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.ansi = False
        self.interactive = False
        self._posix_state = None

    def size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size()
            return max(MIN_WIDTH, size.columns - 1), max(20, size.lines)
        except OSError:
            return DEFAULT_WIDTH, DEFAULT_HEIGHT

    def __enter__(self):
        self.ansi = _enable_vt()
        self.interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
        if self.ansi:
            self.stream.write("\033[?1049h\033[?25l")  # alternate screen, hide cursor
            self.stream.flush()
        if self.interactive and os.name != "nt":
            try:
                import termios
                import tty

                fd = sys.stdin.fileno()
                self._posix_state = (fd, termios.tcgetattr(fd))
                tty.setcbreak(fd)
            except Exception:  # noqa: BLE001 - keys are a convenience, not the feature
                self._posix_state = None
                self.interactive = False
        return self

    def __exit__(self, *exc):
        if self._posix_state:
            try:
                import termios

                fd, saved = self._posix_state
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except Exception:  # noqa: BLE001
                pass
        if self.ansi:
            self.stream.write("\033[?25h\033[?1049l")  # show cursor, leave alt screen
            self.stream.flush()
        return False

    def draw(self, lines: list[str]) -> None:
        if self.ansi:
            self.stream.write("\033[H\033[2J" + "\n".join(lines) + "\n")
        else:
            self.stream.write("\n\n" + "\n".join(lines) + "\n")
        self.stream.flush()

    def wait_key(self, timeout: float) -> str | None:
        """One keypress within `timeout`, or None. Never blocks past the timeout."""
        if not self.interactive:
            time.sleep(max(0.0, timeout))
            return None
        deadline = time.time() + max(0.0, timeout)
        if os.name == "nt":
            import msvcrt

            while time.time() < deadline:
                if msvcrt.kbhit():
                    try:
                        return msvcrt.getwch()
                    except Exception:  # noqa: BLE001
                        return None
                time.sleep(0.05)
            return None
        import select

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([sys.stdin], [], [], remaining)
            if ready:
                return sys.stdin.read(1)


def key_action(key: str | None) -> tuple[str, int | None]:
    """The keymap, as a pure function: (action, argument).

    Lifted out of the loop so the offline gate can drive it - a keymap that can
    only be tested by sitting at a terminal is a keymap nobody re-checks after
    changing it. Actions: quit / reload / open / unknown.
    """
    if key is None:
        return "reload", None
    if key in ("q", "Q", "\x03", "\x1b"):  # q, Ctrl-C, Esc
        return "quit", None
    if key in ("r", "R", "\r", "\n", " "):
        return "reload", None
    if key.isdigit() and key != "0":
        return "open", int(key)
    return "unknown", None


def open_pr(snapshot: dict, index: int) -> str:
    """Open the nth queued PR in a browser. Returns what happened, for the status row."""
    prs = section(snapshot, "pull_requests").get("pull_requests") or []
    if not 1 <= index <= len(prs):
        return f"no PR {index} in the queue"
    url = (prs[index - 1] or {}).get("url")
    if not url:
        return "that PR has no URL recorded"
    try:
        webbrowser.open(url)
        return f"opened {url}"
    except Exception as e:  # noqa: BLE001 - no browser is not a crash
        return f"could not open a browser ({type(e).__name__}) - {url}"


def watch(collectors: dict, meta: dict | None = None, interval: float = DEFAULT_INTERVAL_S,
          once: bool = False, as_json: bool = False) -> int:
    """The loop. Returns a process exit code (always 0 - a screen is a reader)."""
    meta = dict(meta or {})
    interval = max(MIN_INTERVAL_S, float(interval))
    meta["interval"] = interval

    if as_json or once or not sys.stdout.isatty():
        snapshot = build_snapshot(collectors)
        if as_json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
        else:
            meta["static"] = True
            print("\n".join(render(snapshot, meta, *Screen().size())))
        return 0

    with Screen() as screen:
        status = ""
        while True:
            snapshot = build_snapshot(collectors)
            width, height = screen.size()
            frame = render(snapshot, meta, width, height)
            if status:
                frame = frame + [clip("  " + status, width)]
            screen.draw(frame)
            action, argument = key_action(screen.wait_key(interval))
            if action == "quit":
                return 0
            if action == "open":
                status = open_pr(snapshot, argument)
            elif action == "unknown":
                status = "unknown key - r reload, 1-9 open a PR, q quit"
            else:
                status = ""
