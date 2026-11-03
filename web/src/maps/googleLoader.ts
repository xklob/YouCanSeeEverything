/** Single page-lifetime loader. Never persists or logs the browser API key. */
declare global {
  interface Window {
    __biographyGoogleReady?: () => void;
    gm_authFailure?: () => void;
  }
}

export const GOOGLE_MAP_ERROR = "biography-google-map-error";
let loading: Promise<typeof google.maps> | undefined;

export function loadGoogleMaps(apiKey: string): Promise<typeof google.maps> {
  if (loading) return loading;
  if (!apiKey)
    return Promise.reject(new Error("Google Maps is not configured."));
  if (window.google?.maps?.Map) return Promise.resolve(window.google.maps);
  loading = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.id = "biography-google-maps";
    script.async = true;
    script.referrerPolicy = "origin";
    let settled = false;
    const fail = () => {
      window.dispatchEvent(new Event(GOOGLE_MAP_ERROR));
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      delete window.__biographyGoogleReady;
      script.remove();
      reject(
        new Error(
          "Google Maps could not load. OpenStreetMap is still available.",
        ),
      );
    };
    const timeout = window.setTimeout(fail, 20000);
    window.gm_authFailure = fail;
    window.__biographyGoogleReady = () => {
      if (settled) return;
      if (!window.google?.maps?.Map) {
        fail();
        return;
      }
      settled = true;
      window.clearTimeout(timeout);
      delete window.__biographyGoogleReady;
      resolve(window.google.maps);
    };
    script.onerror = fail;
    const params = new URLSearchParams({
      key: apiKey,
      callback: "__biographyGoogleReady",
      loading: "async",
      v: "quarterly",
    });
    script.src = `https://maps.googleapis.com/maps/api/js?${params}`;
    document.head.append(script);
  });
  return loading;
}
