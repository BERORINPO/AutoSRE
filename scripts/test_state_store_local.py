"""Offline smoke test for the durable run-budget store (state_store).

A fake google.cloud.storage is injected, so this exercises the real CAS loop,
the failure policy and the pure decision function without GCP or network.

Why this exists: the auto-trigger cooldown used to live in a module global.
With min-instances=0 and maxScale=20 a cold start reset it and every instance
kept its own copy, so the intended 288 runs/day ceiling was really 5,760.

Usage:
    python scripts/test_state_store_local.py
"""
import os
import sys
import types

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "packages", "agent", "src")
)

URI = "gs://test-bucket/state.json"
DAY_S = 86400.0

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


# ------------------------------------------------------------- fake GCS SDK
class NotFound(Exception):
    pass


class PreconditionFailed(Exception):
    pass


class _FakeBlob:
    """Minimal Blob: generation-based CAS over an in-memory backing store."""

    def __init__(self, store: "_FakeBackend", name: str):
        self._store = store
        self.name = name
        self.generation = None

    def reload(self, timeout=None):
        self._store.reads += 1
        if self._store.fail_read:
            raise RuntimeError("simulated GCS read failure")
        if self._store.data is None:
            raise NotFound(self.name)
        self.generation = self._store.generation

    def download_as_bytes(self, if_generation_match=None, timeout=None):
        if self._store.data is None:
            raise NotFound(self.name)
        if if_generation_match is not None and if_generation_match != self._store.generation:
            raise PreconditionFailed("generation moved")
        return self._store.data

    def upload_from_string(self, data, content_type=None, if_generation_match=None, timeout=None):
        self._store.writes += 1
        if self._store.fail_write:
            raise RuntimeError("simulated GCS write failure")
        current = 0 if self._store.data is None else self._store.generation
        if if_generation_match is not None and if_generation_match != current:
            raise PreconditionFailed("generation moved")
        # A competing writer lands between our read and our write, exactly once.
        if self._store.contend_once:
            self._store.contend_once = False
            self._store.generation += 1
            raise PreconditionFailed("lost the race")
        self._store.data = data.encode("utf-8") if isinstance(data, str) else data
        self._store.generation = current + 1


class _FakeBackend:
    def __init__(self):
        self.data = None
        self.generation = 0
        self.reads = 0
        self.writes = 0
        self.fail_read = False
        self.fail_write = False
        self.contend_once = False

    def bucket(self, name):
        self.bucket_name = name
        return self

    def blob(self, name):
        return _FakeBlob(self, name)


BACKEND = _FakeBackend()


def _install_fake_storage() -> None:
    storage = types.ModuleType("google.cloud.storage")
    storage.Client = lambda *a, **k: BACKEND
    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    setattr(google, "cloud", cloud)
    setattr(cloud, "storage", storage)
    sys.modules["google.cloud.storage"] = storage

    exceptions = types.ModuleType("google.api_core.exceptions")
    exceptions.NotFound = NotFound
    exceptions.PreconditionFailed = PreconditionFailed
    sys.modules["google.api_core.exceptions"] = exceptions


_install_fake_storage()
import agents.state_store as st  # noqa: E402


def reset(**env) -> None:
    BACKEND.__init__()
    for k in ("AUTOSRE_STATE_URI", "AUTOSRE_AUTO_COOLDOWN_S", "AUTOSRE_DAILY_RUN_LIMIT"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


# ------------------------------------------------------- pure decision logic
def test_evaluate() -> None:
    now = 1_800_000_000.0
    fresh = st.empty_state()

    ok, reason, nxt = st.evaluate(fresh, now, 300.0, 50)
    check("evaluate: first run allowed", ok and reason == "ok", f"{ok} {reason}")
    check("evaluate: counts the run", nxt["runs_today"] == 1, f"{nxt['runs_today']}")
    check("evaluate: stamps last_run_ts", nxt["last_run_ts"] == now, f"{nxt['last_run_ts']}")

    ok, reason, _ = st.evaluate(nxt, now + 10, 300.0, 50)
    check("evaluate: inside cooldown denied", (not ok) and reason == "cooldown", f"{ok} {reason}")

    ok, reason, _ = st.evaluate(nxt, now + 301, 300.0, 50)
    check("evaluate: past cooldown allowed", ok and reason == "ok", f"{ok} {reason}")

    at_limit = {**fresh, "day": st.day_key(now), "runs_today": 50, "last_run_ts": 0.0}
    ok, reason, nxt2 = st.evaluate(at_limit, now, 300.0, 50)
    check("evaluate: daily limit denied", (not ok) and reason == "daily_limit", f"{ok} {reason}")
    check(
        "evaluate: daily limit self-trips the kill switch",
        nxt2["killswitch"]["tripped"] is True and "50/50" in nxt2["killswitch"]["reason"],
        f"{nxt2['killswitch']}",
    )

    tripped = {**fresh, "killswitch": {"tripped": True, "reason": "manual", "ts": now}}
    ok, reason, _ = st.evaluate(tripped, now + DAY_S, 300.0, 50)
    check(
        "evaluate: kill switch outranks a fresh day",
        (not ok) and reason == "killswitch",
        f"{ok} {reason} - an explicit stop must not expire on its own",
    )

    used = {**fresh, "day": st.day_key(now), "runs_today": 50}
    ok, reason, nxt3 = st.evaluate(used, now + DAY_S, 300.0, 50)
    check(
        "evaluate: budget resets on a new UTC day",
        ok and nxt3["runs_today"] == 1 and nxt3["day"] == st.day_key(now + DAY_S),
        f"{ok} {reason} {nxt3.get('runs_today')}",
    )

    ok, _, _ = st.evaluate({}, now, 300.0, 50)
    check("evaluate: tolerates an empty/partial state", ok, "must not raise on {}")


# ----------------------------------------------------------------- CAS loop
def test_reserve_run() -> None:
    reset()
    allowed, reason = st.reserve_run()
    check(
        "disabled by default (no AUTOSRE_STATE_URI)",
        allowed and reason == "disabled",
        f"{allowed} {reason} - unset must not change behaviour",
    )
    check("disabled does no I/O", BACKEND.reads == 0 and BACKEND.writes == 0, "")

    reset(AUTOSRE_STATE_URI=URI, AUTOSRE_AUTO_COOLDOWN_S="300", AUTOSRE_DAILY_RUN_LIMIT="3")
    now = 1_800_000_000.0
    a1 = st.reserve_run(now)
    check("first reserve allowed (object created)", a1 == (True, "ok"), f"{a1}")
    check("state object was written", BACKEND.data is not None, "")

    a2 = st.reserve_run(now + 5)
    check("second reserve inside cooldown denied", a2 == (False, "cooldown"), f"{a2}")

    a3 = st.reserve_run(now + 400)
    a4 = st.reserve_run(now + 800)
    check("reserves past cooldown allowed", a3 == (True, "ok") and a4 == (True, "ok"), f"{a3} {a4}")

    a5 = st.reserve_run(now + 1200)
    check("daily limit denies the 4th run", a5 == (False, "daily_limit"), f"{a5}")

    a6 = st.reserve_run(now + 2000)
    check(
        "after the limit trips, later runs are denied by the kill switch",
        a6 == (False, "killswitch"),
        f"{a6} - the trip must persist, not just block one call",
    )

    ok = st.set_killswitch(False, "cleared by operator", now + 2100)
    a7 = st.reserve_run(now + 2200)
    check("manual clear works", ok and a7[0] is False and a7[1] == "daily_limit", f"{ok} {a7}")


def test_cas_contention() -> None:
    reset(AUTOSRE_STATE_URI=URI, AUTOSRE_AUTO_COOLDOWN_S="0", AUTOSRE_DAILY_RUN_LIMIT="10")
    now = 1_800_000_000.0
    st.reserve_run(now)
    BACKEND.contend_once = True
    allowed, reason = st.reserve_run(now + 1)
    check(
        "a lost CAS race is retried, not dropped",
        (allowed, reason) == (True, "ok"),
        f"{allowed} {reason}",
    )
    state = st.read_state()
    check(
        "the retry did not double-count the run",
        state["runs_today"] == 2,
        f"runs_today={state['runs_today']} (expected 2: one per successful reserve)",
    )


def test_failure_policy() -> None:
    reset(AUTOSRE_STATE_URI=URI, AUTOSRE_DAILY_RUN_LIMIT="10")
    BACKEND.fail_read = True
    allowed, reason = st.reserve_run()
    check(
        "backend unreachable -> allow + 'unavailable'",
        (allowed, reason) == (True, "unavailable"),
        f"{allowed} {reason} - denying every incident because bookkeeping is down "
        "would take the agent offline; the caller falls back in-process",
    )

    snapshot = st.read_state()
    check(
        "read_state reports unavailability instead of raising",
        snapshot["enabled"] is True and snapshot["available"] is False,
        f"{snapshot}",
    )

    reset(AUTOSRE_STATE_URI="not-a-gs-uri")
    allowed, reason = st.reserve_run()
    check(
        "malformed URI -> disabled, not a crash",
        (allowed, reason) == (True, "disabled"),
        f"{allowed} {reason}",
    )

    reset(AUTOSRE_STATE_URI=URI)
    BACKEND.data = b"{ this is not json"
    BACKEND.generation = 7
    allowed, reason = st.reserve_run()
    check(
        "corrupt state object does not wedge the agent",
        (allowed, reason) == (True, "ok"),
        f"{allowed} {reason} - a bad object must be recoverable, not permanent",
    )


def test_env_parsing() -> None:
    reset(AUTOSRE_STATE_URI=URI, AUTOSRE_DAILY_RUN_LIMIT="abc", AUTOSRE_AUTO_COOLDOWN_S="abc")
    check("malformed daily limit -> default", st.daily_limit() == st.DEFAULT_DAILY_LIMIT, "")
    check("malformed cooldown -> default", st.cooldown_s() == st.DEFAULT_COOLDOWN_S, "")

    reset(AUTOSRE_STATE_URI=URI, AUTOSRE_DAILY_RUN_LIMIT="0")
    check(
        "zero daily limit -> default (never an accidental total block)",
        st.daily_limit() == st.DEFAULT_DAILY_LIMIT,
        f"{st.daily_limit()}",
    )

    reset(AUTOSRE_STATE_URI=URI, AUTOSRE_AUTO_COOLDOWN_S="0")
    check("cooldown 0 is honoured (explicitly off)", st.cooldown_s() == 0.0, f"{st.cooldown_s()}")


def main() -> int:
    test_evaluate()
    test_reserve_run()
    test_cas_contention()
    test_failure_policy()
    test_env_parsing()
    reset()

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f" - {detail}"
        print(line)
    print(f"\n{passed}/{len(results)} pass")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
