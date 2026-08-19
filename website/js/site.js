/* coldscreen site: copy button, scroll reveal, nav hairline.
   Everything degrades to a fully readable page without JS. */
(function () {
  "use strict";

  document.documentElement.classList.add("js");

  /* ----- copy-to-clipboard ----- */
  var buttons = document.querySelectorAll("button[data-copy]");
  Array.prototype.forEach.call(buttons, function (btn) {
    var idle = btn.textContent;
    var timer = null;

    function settle(label, copied) {
      btn.textContent = label;
      btn.classList.toggle("is-copied", copied);
      if (timer) { window.clearTimeout(timer); }
      timer = window.setTimeout(function () {
        btn.textContent = idle;
        btn.classList.remove("is-copied");
      }, 2000);
    }

    function legacyCopy(text) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "absolute";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(area);
      return ok;
    }

    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { settle("Copied", true); },
          function () {
            var ok = legacyCopy(text);
            settle(ok ? "Copied" : "Copy failed", ok);
          }
        );
      } else {
        var ok = legacyCopy(text);
        settle(ok ? "Copied" : "Copy failed", ok);
      }
    });
  });

  /* ----- reveal on scroll ----- */
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (!reduce.matches && "IntersectionObserver" in window) {
    var targets = document.querySelectorAll("[data-reveal]");
    /* Arm the hidden state only now that the animation is certain to run. */
    document.documentElement.classList.add("js-reveal-armed");

    /* Anything at or above the fold, or already scrolled past, is shown
       immediately: an observer callback is not guaranteed for elements that
       never cross the boundary while it is watching, and content must never
       depend on an animation firing. */
    var settleIfPastOrVisible = function (el) {
      var rect = el.getBoundingClientRect();
      if (rect.top < window.innerHeight) {
        el.classList.add("is-in");
        return true;
      }
      return false;
    };

    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-in");
            io.unobserve(entry.target);
          }
        });
      },
      { rootMargin: "0px 0px -5% 0px", threshold: 0 }
    );

    Array.prototype.forEach.call(targets, function (el) {
      el.classList.add("will-reveal");
      if (!settleIfPastOrVisible(el)) {
        io.observe(el);
      }
    });

    /* Safety net: if anything is still hidden after the page settles, show
       it. The animation is decoration; the words are the point. */
    window.setTimeout(function () {
      Array.prototype.forEach.call(targets, settleIfPastOrVisible);
    }, 1200);
  }

  /* ----- nav hairline once the page scrolls ----- */
  var nav = document.getElementById("nav");
  if (nav) {
    var onScroll = function () {
      nav.classList.toggle("is-scrolled", window.scrollY > 8);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }
})();
