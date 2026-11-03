import { afterEach, expect, it, vi } from "vitest";
afterEach(() => {
  document.getElementById("biography-google-maps")?.remove();
  vi.unstubAllGlobals();
  delete window.__biographyGoogleReady;
  delete window.gm_authFailure;
  vi.useRealTimers();
});

it("appends a single script for simultaneous requests and never persists a key", async () => {
  vi.resetModules();
  const { loadGoogleMaps } = await import("../src/maps/googleLoader");
  const set = vi.spyOn(Storage.prototype, "setItem");
  const first = loadGoogleMaps("synthetic-browser-key");
  const second = loadGoogleMaps("synthetic-browser-key");
  expect(second).toBe(first);
  expect(document.querySelectorAll("#biography-google-maps")).toHaveLength(1);
  const script = document.getElementById(
    "biography-google-maps",
  ) as HTMLScriptElement;
  expect(new URL(script.src).searchParams.has("auth_referrer_policy")).toBe(
    false,
  );
  expect(script.referrerPolicy).toBe("origin");
  vi.stubGlobal("google", { maps: { Map: function () {} } });
  window.__biographyGoogleReady?.();
  await expect(first).resolves.toBe(window.google.maps);
  expect(set).not.toHaveBeenCalled();
  set.mockRestore();
});

it("uses a generic error when loading fails, without retrying another script", async () => {
  vi.resetModules();
  const { loadGoogleMaps } = await import("../src/maps/googleLoader");
  const promise = loadGoogleMaps("synthetic-browser-key");
  const assertion = expect(promise).rejects.toThrow(
    "Google Maps could not load.",
  );
  document
    .getElementById("biography-google-maps")
    ?.dispatchEvent(new Event("error"));
  await assertion;
  expect(loadGoogleMaps("synthetic-browser-key")).toBe(promise);
  expect(document.querySelector("#biography-google-maps")).toBeNull();
});
