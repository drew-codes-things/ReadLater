import logging
import threading

from .library import LibraryError, iso

log = logging.getLogger("readlater.linkcheck")


def _idle_state() -> dict:
    return {"running": False, "total": 0, "checked": 0, "dead": 0, "startedAt": None, "finishedAt": None, "error": ""}


class LinkChecker:
    def __init__(self, saver):
        self.saver = saver
        self.library = saver.library
        self._lock = threading.Lock()
        self._thread = None
        self._state = _idle_state()

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, view: str = "all") -> dict:
        with self._lock:
            if self._state["running"]:
                return dict(self._state)
            targets = self.library.items_to_check(view)
            self._state = {**_idle_state(), "running": True, "total": len(targets), "startedAt": iso(self.library.clock())}
            self._thread = threading.Thread(target=self._run, args=(targets,), name="link-check", daemon=True)
            self._thread.start()
            return dict(self._state)

    def wait(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _check_one(self, target: dict) -> None:
        try:
            item = self.saver.check_link(target["id"])
        except LibraryError as error:
            if error.status != 404:
                raise
            log.info("link check: item %s was deleted before its turn, skipped", target["id"])
            return
        with self._lock:
            self._state["checked"] += 1
            if item["linkStatus"] == "dead":
                self._state["dead"] += 1

    def _run(self, targets: list[dict]) -> None:
        try:
            for target in targets:
                self._check_one(target)
        except Exception as error:
            log.exception("link check stopped early")
            with self._lock:
                self._state["error"] = str(error) or type(error).__name__
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["finishedAt"] = iso(self.library.clock())
