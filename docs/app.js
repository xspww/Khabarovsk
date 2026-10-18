/* Reveal-on-scroll for the Cronus Launcher landing page.
   The `js` class is set inline in <head> so nothing flashes: elements start
   hidden only when this script can actually reveal them again. */
(() => {
  const items = Array.from(document.querySelectorAll(".reveal"));
  if (!items.length) return;

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || !("IntersectionObserver" in window)) {
    for (const el of items) el.classList.add("in");
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add("in");
        observer.unobserve(entry.target);
      }
    },
    { rootMargin: "0px 0px -10% 0px", threshold: 0.06 }
  );

  for (const el of items) observer.observe(el);
})();
