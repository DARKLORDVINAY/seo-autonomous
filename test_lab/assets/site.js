"use strict";

// Public analytics identifiers are optional. No remote code loads before consent.
(() => {
  const id = document.querySelector('meta[name="lab-ga4-measurement-id"]')?.content || "";
  const validId = /^G-[A-Z0-9]{10}$/.test(id);
  const allow = document.getElementById("analytics-allow");
  const decline = document.getElementById("analytics-decline");
  const status = document.getElementById("analytics-status");
  let analyticsAllowed = false;
  let completed = false;
  let emitted = false;

  function referrerOrigin() {
    try {
      const referrer = new URL(document.referrer);
      return /^https?:$/.test(referrer.protocol) ? referrer.origin + "/" : "";
    } catch (_) {
      return "";
    }
  }

  function allowAnalytics() {
    if (!validId || analyticsAllowed) return;
    analyticsAllowed = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    window.gtag("consent", "default", {
      analytics_storage: "granted",
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied"
    });
    window.gtag("js", new Date());
    window.gtag("config", id, {
      send_page_view: false,
      allow_google_signals: false,
      allow_ad_personalization_signals: false,
      page_location: window.location.origin + window.location.pathname,
      page_referrer: referrerOrigin()
    });
    window.gtag("event", "page_view", {
      page_location: window.location.origin + window.location.pathname,
      page_referrer: referrerOrigin()
    });
    const script = document.createElement("script");
    script.async = true;
    script.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
    script.addEventListener("error", () => {
      status.textContent = "The test analytics library could not load. Your local practice result is still available.";
    });
    document.head.appendChild(script);
    status.textContent = "Test analytics is allowed for this page visit. A page-view event was queued; new practice completions can queue the test event. Reload to return to analytics off.";
    allow.disabled = true;
    decline.disabled = true;
  }

  allow?.addEventListener("click", allowAnalytics);
  decline?.addEventListener("click", () => {
    status.textContent = "Test analytics stays off. The practice checklist works locally.";
    allow.disabled = true;
    decline.disabled = true;
  });

  const checks = Array.from(document.querySelectorAll('input[name="lab-step"]'));
  const complete = document.getElementById("complete-checklist");
  const result = document.getElementById("exercise-result");
  if (!complete || checks.length !== 3) return;

  function updateReadiness() {
    complete.disabled = completed || !checks.every((check) => check.checked);
    if (!completed) {
      result.textContent = complete.disabled
        ? "Complete all three checks to enable the button."
        : "All three checks are complete. You can record this practice result.";
    }
  }
  checks.forEach((check) => check.addEventListener("change", updateReadiness));
  complete.addEventListener("click", () => {
    if (completed || checks.length !== 3 || !checks.every((check) => check.checked)) return;
    completed = true;
    complete.disabled = true;
    checks.forEach((check) => { check.disabled = true; });
    if (validId && analyticsAllowed && typeof window.gtag === "function" && !emitted) {
      emitted = true;
      window.gtag("event", "lab_checklist_complete", {
        lab_mode: true,
        lab_exercise: "page-review"
      });
      result.textContent = "Practice complete. One lab_checklist_complete test event was queued; receipt by analytics has not been verified.";
    } else {
      result.textContent = "Practice complete. Your result is local; no analytics event was sent.";
    }
  });
  updateReadiness();
})();
