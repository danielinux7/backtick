// Deterministic character avatars — no assets, no deps. Each account is mapped
// (by a stable hash of its id) to one of a fixed gallery of cute SVG creatures,
// so a given user always gets the same one. Guests get a distinct ghost on a
// muted slate tile. Consumed by app.js renderUserInfo() via window.BTAvatars.
(function () {
  "use strict";

  const D = "#11161f"; // dark facial features — reads well on every vivid tile

  // FNV-1a → stable, well-spread 32-bit hash so ids fan out across the gallery.
  function hash(s) {
    s = String(s == null ? "" : s);
    let h = 2166136261 >>> 0;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return h >>> 0;
  }

  function wrap(id, c1, c2, inner) {
    return (
      '<svg viewBox="0 0 36 36" width="100%" height="100%" preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false">' +
      '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="1" y2="1">' +
      '<stop offset="0" stop-color="' + c1 + '"/><stop offset="1" stop-color="' + c2 + '"/>' +
      '</linearGradient></defs>' +
      '<circle cx="18" cy="18" r="18" fill="url(#' + id + ')"/>' +
      inner +
      "</svg>"
    );
  }

  // Each entry: [colorA, colorB, facialFeatures]. Index order is the gallery
  // order; appending new ones is safe (existing users keep theirs unless the
  // gallery length changes, which only reshuffles — fine for cosmetic avatars).
  const GALLERY = [
    // robot
    ["#2dd4bf", "#06b6d4",
      '<line x1="18" y1="3.5" x2="18" y2="7.5" stroke="' + D + '" stroke-width="1.4" stroke-linecap="round"/>' +
      '<circle cx="18" cy="3" r="1.7" fill="' + D + '"/>' +
      '<rect x="10.5" y="14" width="5" height="5" rx="1.3" fill="' + D + '"/>' +
      '<rect x="20.5" y="14" width="5" height="5" rx="1.3" fill="' + D + '"/>' +
      '<rect x="13" y="23" width="10" height="2" rx="1" fill="' + D + '"/>'],
    // cat
    ["#8b5cf6", "#6366f1",
      '<path d="M8 6 L14 12 L7.5 13 Z" fill="' + D + '"/>' +
      '<path d="M28 6 L22 12 L28.5 13 Z" fill="' + D + '"/>' +
      '<circle cx="13.5" cy="17" r="2.3" fill="' + D + '"/>' +
      '<circle cx="22.5" cy="17" r="2.3" fill="' + D + '"/>' +
      '<path d="M16.5 21.5 Q18 23 19.5 21.5" stroke="' + D + '" stroke-width="1.3" fill="none" stroke-linecap="round"/>' +
      '<line x1="5" y1="18" x2="11" y2="18.5" stroke="' + D + '" stroke-width="0.9" stroke-linecap="round"/>' +
      '<line x1="25" y1="18.5" x2="31" y2="18" stroke="' + D + '" stroke-width="0.9" stroke-linecap="round"/>'],
    // bear
    ["#ec4899", "#f43f5e",
      '<circle cx="11" cy="9.5" r="3.3" fill="' + D + '"/>' +
      '<circle cx="25" cy="9.5" r="3.3" fill="' + D + '"/>' +
      '<circle cx="13.5" cy="17" r="2.2" fill="' + D + '"/>' +
      '<circle cx="22.5" cy="17" r="2.2" fill="' + D + '"/>' +
      '<circle cx="18" cy="22.5" r="1.5" fill="' + D + '"/>'],
    // fox
    ["#f59e0b", "#f97316",
      '<path d="M8 7 L14.5 13 L8 14.5 Z" fill="' + D + '"/>' +
      '<path d="M28 7 L21.5 13 L28 14.5 Z" fill="' + D + '"/>' +
      '<circle cx="13.5" cy="17.5" r="2" fill="' + D + '"/>' +
      '<circle cx="22.5" cy="17.5" r="2" fill="' + D + '"/>' +
      '<path d="M18 21 L16.3 23 L19.7 23 Z" fill="' + D + '"/>'],
    // bunny
    ["#38bdf8", "#3b82f6",
      '<rect x="12" y="2.5" width="3.4" height="11" rx="1.7" fill="' + D + '"/>' +
      '<rect x="20.6" y="2.5" width="3.4" height="11" rx="1.7" fill="' + D + '"/>' +
      '<circle cx="13.8" cy="18" r="2.1" fill="' + D + '"/>' +
      '<circle cx="22.2" cy="18" r="2.1" fill="' + D + '"/>' +
      '<circle cx="18" cy="22.5" r="1.3" fill="' + D + '"/>'],
    // owl
    ["#d946ef", "#a855f7",
      '<circle cx="13.5" cy="16.5" r="4" fill="none" stroke="' + D + '" stroke-width="1.6"/>' +
      '<circle cx="22.5" cy="16.5" r="4" fill="none" stroke="' + D + '" stroke-width="1.6"/>' +
      '<circle cx="13.5" cy="16.5" r="1.5" fill="' + D + '"/>' +
      '<circle cx="22.5" cy="16.5" r="1.5" fill="' + D + '"/>' +
      '<path d="M18 20.5 L16.4 23 L19.6 23 Z" fill="' + D + '"/>'],
    // alien
    ["#84cc16", "#22c55e",
      '<ellipse cx="13" cy="17" rx="2.3" ry="3.6" fill="' + D + '" transform="rotate(-14 13 17)"/>' +
      '<ellipse cx="23" cy="17" rx="2.3" ry="3.6" fill="' + D + '" transform="rotate(14 23 17)"/>' +
      '<path d="M14.5 24 Q18 26 21.5 24" stroke="' + D + '" stroke-width="1.2" fill="none" stroke-linecap="round"/>'],
    // cyclops bot
    ["#ef4444", "#ec4899",
      '<line x1="18" y1="3.5" x2="18" y2="7.5" stroke="' + D + '" stroke-width="1.4" stroke-linecap="round"/>' +
      '<circle cx="18" cy="3" r="1.7" fill="' + D + '"/>' +
      '<circle cx="18" cy="17" r="5" fill="none" stroke="' + D + '" stroke-width="1.7"/>' +
      '<circle cx="18" cy="17" r="2.1" fill="' + D + '"/>' +
      '<rect x="13.5" y="25" width="9" height="1.8" rx="0.9" fill="' + D + '"/>'],
    // cool / sunglasses
    ["#10b981", "#14b8a6",
      '<rect x="9" y="14" width="7.5" height="5.2" rx="1.6" fill="' + D + '"/>' +
      '<rect x="19.5" y="14" width="7.5" height="5.2" rx="1.6" fill="' + D + '"/>' +
      '<rect x="16" y="15.5" width="4" height="1.5" fill="' + D + '"/>' +
      '<path d="M14 24 Q18 27 22 24" stroke="' + D + '" stroke-width="1.6" fill="none" stroke-linecap="round"/>'],
    // frog
    ["#3b82f6", "#8b5cf6",
      '<circle cx="12" cy="11" r="3.6" fill="' + D + '"/>' +
      '<circle cx="24" cy="11" r="3.6" fill="' + D + '"/>' +
      '<circle cx="12.8" cy="10.2" r="1.1" fill="#fff"/>' +
      '<circle cx="24.8" cy="10.2" r="1.1" fill="#fff"/>' +
      '<path d="M11 19 Q18 24 25 19" stroke="' + D + '" stroke-width="1.7" fill="none" stroke-linecap="round"/>'],
    // sleepy / zen
    ["#fb7185", "#fb923c",
      '<path d="M11 16.5 Q13.5 19 16 16.5" stroke="' + D + '" stroke-width="1.6" fill="none" stroke-linecap="round"/>' +
      '<path d="M20 16.5 Q22.5 19 25 16.5" stroke="' + D + '" stroke-width="1.6" fill="none" stroke-linecap="round"/>' +
      '<path d="M15 23 Q18 25 21 23" stroke="' + D + '" stroke-width="1.5" fill="none" stroke-linecap="round"/>'],
    // sparkle
    ["#22d3ee", "#2dd4bf",
      '<path d="M18 8 L20 16 L28 18 L20 20 L18 28 L16 20 L8 18 L16 16 Z" fill="' + D + '"/>'],
  ];

  function forAccount(me) {
    const seed = me && me.id != null ? "id" + me.id : "em" + ((me && me.email) || "");
    const i = hash(seed) % GALLERY.length;
    const a = GALLERY[i];
    return wrap("bta" + i, a[0], a[1], a[2]);
  }

  function guest() {
    // Friendly ghost on a muted slate tile — clearly distinct from the vivid
    // account gallery, so anonymous sessions read as "guest" at a glance.
    return wrap(
      "btag", "#64748b", "#475569",
      '<path d="M10.5 26 V16.5 a7.5 7.5 0 0 1 15 0 V26 q-1.875 -2.5 -3.75 0 q-1.875 2.5 -3.75 0 q-1.875 -2.5 -3.75 0 q-1.875 2.5 -3.75 0 Z" fill="#e8edf3"/>' +
      '<circle cx="15.5" cy="17" r="1.6" fill="#3a4658"/>' +
      '<circle cx="20.5" cy="17" r="1.6" fill="#3a4658"/>'
    );
  }

  window.BTAvatars = { forAccount: forAccount, guest: guest };
})();
