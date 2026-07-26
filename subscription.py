#!/usr/bin/env python3
"""
Scope subscription layer.

Lazy default implementation per architecture doc:
- Every time an agent queries the graph, log {agent_id, symbol, timestamp}
- When Agent A emits a delta, check dict for agents that queried changed symbols
  in the last WINDOW_SECONDS
- If found, queue a notification
- pending_notifications flushed when agent signals task_complete()

~50 lines of logic. Covers sequential agents and the simple concurrent case.
"""

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable


WINDOW_SECONDS = 300  # 5 minutes


@dataclass
class Notification:
    agent_id: str
    changed_file: str
    changed_symbols: list[str]
    new_src: str          # full new source of changed file
    timestamp: float = field(default_factory=time.time)

    def summary(self) -> str:
        return (f"[NOTIFICATION → {self.agent_id}] "
                f"{self.changed_file} changed: {self.changed_symbols}")


class SubscriptionBus:
    """
    Tracks agent symbol reads and routes deltas to interested agents.

    Usage:
        bus = SubscriptionBus()
        bus.record_read("agent_b", "createBudgetStore")   # called by graph query layer
        ...
        notifs = bus.emit_delta("agent_a", "lib/budget-signals.ts",
                                ["createBudgetStore"], new_src)
        # notifs → [Notification(agent_id="agent_b", ...)]
        
    For persistence (crash recovery):
        bus = SubscriptionBus(persistent=True, db_path=Path("data/cone.db"))
        # State is auto-saved on record_read, emit_delta, flush
        # On restart, state is auto-restored from SQLite
    """

    def __init__(self, window: int = WINDOW_SECONDS, persistent: bool = False, db_path=None):
        self.window = window
        self.persistent = persistent
        self._store = None
        
        # {agent_id: [(symbol, timestamp), ...]}
        self._reads: dict[str, list[tuple[str, float]]] = defaultdict(list)
        # {agent_id: [Notification, ...]}  — queued until agent signals done
        self._pending: dict[str, list[Notification]] = defaultdict(list)
        # optional callback: called immediately when a notification is queued
        self._on_notify: Callable | None = None
        
        # Initialize persistence if enabled
        if persistent:
            from persistence import SubscriptionStore
            self._store = SubscriptionStore(db_path) if db_path else SubscriptionStore()
            self._load_state()
        # Lock for thread-safe access to _reads and _pending
        self._lock = threading.Lock()

    def _load_state(self) -> None:
        """Load state from persistent store."""
        if self._store:
            loaded_reads = self._store.load_reads()
            for agent_id, read_list in loaded_reads.items():
                self._reads[agent_id] = read_list
            loaded_pending = self._store.load_pending(Notification)
            for agent_id, notif_list in loaded_pending.items():
                self._pending[agent_id] = notif_list

    def _save_state(self) -> None:
        """Save state to persistent store."""
        if self._store:
            self._store.save_reads(dict(self._reads))
            self._store.save_pending(dict(self._pending))

    def record_read(self, agent_id: str, symbol: str) -> None:
        """Log that agent_id has read/reasoned about symbol."""
        with self._lock:
            self._reads[agent_id].append((symbol, time.time()))
        if self.persistent:
            self._save_state()

    def record_reads(self, agent_id: str, symbols: list[str]) -> None:
        with self._lock:
            ts = time.time()
            for s in symbols:
                self._reads[agent_id].append((s, ts))
        if self.persistent:
            self._save_state()

    def emit_delta(self, emitting_agent: str, changed_file: str,
                   changed_symbols: list[str], new_src: str) -> list[Notification]:
        """
        Called when an agent writes a change.
        Returns list of Notifications queued for other agents.
        """
        now = time.time()
        notified = []

        with self._lock:
            for agent_id, reads in list(self._reads.items()):
                if agent_id == emitting_agent:
                    continue
                # check if this agent read any of the changed symbols recently
                recent_symbols = {
                    sym for sym, ts in reads
                    if now - ts <= self.window and sym in changed_symbols
                }
                if recent_symbols:
                    n = Notification(
                        agent_id=agent_id,
                        changed_file=changed_file,
                        changed_symbols=sorted(recent_symbols),
                        new_src=new_src,
                    )
                    self._pending[agent_id].append(n)
                    notified.append(n)

        # Call notification callback outside lock to avoid deadlocks
        if self._on_notify:
            for n in notified:
                self._on_notify(n)

        if self.persistent:
            self._save_state()

        return notified

    def flush(self, agent_id: str) -> list[Notification]:
        """Called when agent signals task_complete. Returns queued notifications."""
        with self._lock:
            pending = self._pending.pop(agent_id, [])
        if self.persistent:
            self._save_state()
        return pending

    def pending_count(self, agent_id: str) -> int:
        with self._lock:
            return len(self._pending.get(agent_id, []))

    def on_notify(self, fn: Callable) -> None:
        """Register a callback for immediate notification (optional)."""
        self._on_notify = fn

    def state(self) -> dict:
        """Debug: current read log and pending queue sizes."""
        with self._lock:
            return {
                "reads": {a: len(v) for a, v in self._reads.items()},
                "pending": {a: len(v) for a, v in self._pending.items()},
            }


# ── Demo / smoke test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    bus = SubscriptionBus(window=300)

    # Simulate Agent B building its context — it reads createBudgetStore
    print("Agent B queries graph for createBudgetStore context...")
    bus.record_reads("agent_b", ["createBudgetStore", "BudgetStore"])

    # Simulate Agent C reading something unrelated
    bus.record_reads("agent_c", ["initStore", "uuid"])

    print(f"Bus state: {bus.state()}")

    # Agent A writes a change
    new_src = "// modified budget-signals.ts with reset()\nexport interface BudgetStore { reset: () => void }"
    print("\nAgent A emits delta for createBudgetStore...")
    notifications = bus.emit_delta(
        emitting_agent="agent_a",
        changed_file="lib/budget-signals.ts",
        changed_symbols=["createBudgetStore", "BudgetStore"],
        new_src=new_src,
    )

    print(f"\nNotifications queued: {len(notifications)}")
    for n in notifications:
        print(f"  {n.summary()}")

    # Agent C is unaffected
    print(f"\nAgent C pending: {bus.pending_count('agent_c')} (expected 0)")
    print(f"Agent B pending: {bus.pending_count('agent_b')} (expected 1)")

    # Agent B finishes its task and flushes
    print("\nAgent B signals task_complete, flushing notifications...")
    flushed = bus.flush("agent_b")
    for n in flushed:
        print(f"  Received: {n.summary()}")
        print(f"  New src preview: {n.new_src[:80]}...")

    print(f"\nAgent B pending after flush: {bus.pending_count('agent_b')} (expected 0)")

    # Test: notification outside window is NOT delivered
    print("\nTesting window expiry (instant override)...")
    bus2 = SubscriptionBus(window=0)  # 0-second window
    bus2.record_reads("agent_b", ["createBudgetStore"])
    time.sleep(0.01)
    notifs2 = bus2.emit_delta("agent_a", "lib/budget-signals.ts", ["createBudgetStore"], "")
    print(f"  Notifications with expired window: {len(notifs2)} (expected 0)")

    print("\n✅ SubscriptionBus smoke test passed")
