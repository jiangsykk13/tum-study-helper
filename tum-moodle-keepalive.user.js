// ==UserScript==
// @name         TUM Keepalive (Moodle + TUMonline)
// @namespace    tum-moodle
// @version      1.1
// @description  Keeps Moodle and TUMonline logged in while one of their tabs is open and the computer is awake.
// @match        https://www.moodle.tum.de/*
// @match        https://campus.tum.de/tumonline/ee/ui/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // Moodle: touch the session via Moodle's own AJAX call (Moodle logs out after 8h idle).
  function keepMoodleAlive() {
    const INTERVAL_MS = 30 * 60 * 1000;

    setInterval(() => {
      const sesskey = window.M && M.cfg && M.cfg.sesskey;
      if (!sesskey) return; // not logged in: nothing to keep alive
      fetch(`/lib/ajax/service.php?sesskey=${sesskey}&info=core_session_touch`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify([{ index: 0, methodname: "core_session_touch", args: {} }]),
      }).catch(() => {});
    }, INTERVAL_MS);
  }

  // TUMonline: reload the page so the app runs its own token refresh (/ee/rest/auth/token/refresh).
  // Its idle timeout is unknown, so refresh often, but never while you are using the tab.
  function keepTumonlineAlive() {
    const CHECK_MS = 60 * 1000;
    const RELOAD_EVERY_MS = 10 * 60 * 1000;
    const IDLE_HIDDEN_MS = 2 * 60 * 1000; // tab in the background
    const IDLE_VISIBLE_MS = 15 * 60 * 1000; // tab in front but untouched

    const loadedAt = Date.now();
    let lastInput = Date.now();
    for (const type of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"]) {
      addEventListener(type, () => (lastInput = Date.now()), { capture: true, passive: true });
    }

    function isEditing() {
      const el = document.activeElement;
      return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
    }

    setInterval(() => {
      const now = Date.now();
      if (now - loadedAt < RELOAD_EVERY_MS) return;
      if (isEditing()) return;
      const idleLimit = document.visibilityState === "hidden" ? IDLE_HIDDEN_MS : IDLE_VISIBLE_MS;
      if (now - lastInput < idleLimit) return;
      location.reload();
    }, CHECK_MS);
  }

  if (location.hostname === "www.moodle.tum.de") keepMoodleAlive();
  else if (location.hostname === "campus.tum.de") keepTumonlineAlive();
})();
