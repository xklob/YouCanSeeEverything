# Optional Google Maps

A configured Google Maps key makes Google the default. Without a key, the app uses OpenStreetMap. Switch either way under **Map → Map provider**. Both show the same records and inferred connections.

## Set up the Google project

1. [Create or select a Google Cloud project](https://console.cloud.google.com/projectcreate) and attach a billing account.
2. Enable **Maps JavaScript API** in the API Library. No Places, Routes, Geocoding or Street View API is needed.
3. Open **APIs & Services → Credentials → Create credentials → API key**.
4. Edit the key. Under application restrictions, choose **Websites** and allow only the local addresses you use:

   ```text
   http://localhost:8000/*
   http://127.0.0.1:8000/*
   ```

5. Under **API restrictions**, select **Restrict key → Maps JavaScript API**, then save. Allow a few minutes for changes to propagate. Add the development port only if you actually use it.

Google's [key setup guide](https://developers.google.com/maps/documentation/javascript/get-api-key) describes these controls. A web key is visible to its browser by design, so the website and API restrictions matter. Keep it out of Git, screenshots, chat and shared logs.

## Configure the local app

Put this setting in a private, ignored environment file outside the application checkout, using your own restricted key as its value:

```text
BIOGRAPHY_GOOGLE_MAPS_API_KEY=YOUR_RESTRICTED_BROWSER_KEY
```

Restrict the file to your user with `chmod 600 /path/to/private/.local/google-maps.env`. If you run the dashboard as a systemd user service, add this line to its service's `[Service]` section, then reload systemd and restart that service:

```ini
EnvironmentFile=-/path/to/private/.local/google-maps.env
```

For a foreground process, load the private setting into that process's environment before starting `uv run biography serve`. Do not place it in a `VITE_` variable or the profile JSON; it is read by the local backend at startup rather than baked into the frontend build.

Refresh the dashboard and open **Map**. Google loads automatically when a key is configured and the background is enabled. A disabled choice means the backend has no key; confirm the environment file and restart the service. If Google rejects the key, the app returns to OpenStreetMap. Check billing, API enablement, both key restrictions and the address in your browser, then refresh to retry.

## Usage and privacy

Google currently includes **10,000 Dynamic Maps loads per month** at no charge, aggregated across projects on the billing account. Usage above that allowance is chargeable; a billing account is still required. Check the [current pricing table](https://developers.google.com/maps/billing-and-pricing/pricing) before enabling it.

The app loads Google automatically when a key is configured and the background is enabled. With no key, it makes no Google requests. Filters and overlay changes reuse the mounted Google map. Refreshing, switching providers, hiding and restoring the background, or returning to the map view can create another load. The app uses neither route computation nor Places lookups; dotted connections are calculated locally.

Selecting Google sends map requests and the viewed region to Google. Returning to OpenStreetMap removes the Google map, but cannot unload scripts already executed in that page; remove the configured key and restart the app to make OSM the default again. Hide the background to browse local overlays without fetching new tiles.

You can set a small project-specific billing alert. An [alerts-only budget](https://docs.cloud.google.com/billing/docs/how-to/budgets) sends notifications and does not cap spending. The free allowance is not a hard spending limit.
