# Google-backed map options

Reviewed November 2026. The application defaults to Leaflet with OpenStreetMap. An optional Google Maps JavaScript adapter displays the same recorded points and inferred connections. Google is loaded only after explicit selection with the background enabled; [setup instructions](google-maps-setup.md) cover its browser key and billing requirements. OpenStreetMap requires no Google account.

The renderer, background-map provider, and overlay data are separate choices. An open-source renderer does not make a commercial provider's tiles or APIs free of their access requirements.

| Option | Custom history overlays | Setup and limitations |
| --- | --- | --- |
| Standard Google Maps share/embed iframe | No supported API for our arbitrary markers and polylines | Can display a shared place or directions. The parent app cannot insert content into the cross-origin iframe or synchronize an independent overlay with its pan/zoom state. |
| Google Maps Embed API | Preset place, view, directions, Street View and search modes | Usage is free and unlimited, but Google requires an API key and billing-enabled project. It does not expose arbitrary overlay controls. |
| Leaflet.GoogleMutant | Yes, Google background with Leaflet overlays | Open-source plugin, but it loads Google's Maps JavaScript API and retains that API's key and billing requirements. |
| vis.gl React Google Maps | Yes, markers, polylines and custom React layers | MIT-licensed React wrapper around the same Google Maps JavaScript API. A Google key and billing-enabled project remain required. |
| OpenLayers Google source / MapLibre with official Google tiles | Yes | Google's Map Tiles API still requires credentials, a session, attribution and billing setup. |
| Google My Maps | Custom imported points and lines | Website embedding requires a public map. Import/map size limits also make it unsuitable as the default for a private lifetime archive. |

Google's current Dynamic Maps allowance includes 10,000 free loads per month, so a personal dashboard may incur no usage charge. That does not remove the billing-account requirement or guarantee a zero bill. The Embed API has a separate unlimited no-charge allowance.

Older plugins sometimes advertise keyless Google backgrounds. For example, `Raruto/leaflet-google` documents compatibility with Google Maps 3.34. Such an old claim is not evidence of a supported current integration. Direct undocumented tile URLs are not used by this application.

For private browsing without Google billing setup, keep the existing map and its locally computed overlays. A normal Google Maps URL can open a selected place in Google's own site without an API key, but it is a separate view rather than the dashboard's background map.

The optional adapter uses Google's supported JavaScript API directly, without an additional runtime wrapper. It reuses the mounted map during filtering and overlay changes. The application does not call Places, Routes or Street View services. Switching providers, hiding and restoring the background, leaving and returning to the map view, or refreshing can create a new map load. A load or authentication failure falls back to OpenStreetMap.

## Trip connections

Dotted lines are inferred straight-line links between chronologically adjacent recorded stops. They are not driving directions or proof of the exact path traveled. The backend establishes adjacency before applying display filters or pagination; missing or implausible time intervals break connections. Posts on the same day remain temporal context rather than inferred location observations.

## Primary references

- [Google Maps share and embed](https://support.google.com/maps/answer/7101463?hl=en)
- [Maps Embed API modes](https://developers.google.com/maps/documentation/embed/embedding-map)
- [Maps Embed API setup and no-charge usage](https://developers.google.com/maps/documentation/embed/quickstart)
- [Google Maps pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
- [Leaflet's Google basemap guidance](https://github.com/Leaflet/Leaflet/blob/main/FAQ.md)
- [Leaflet.GoogleMutant](https://gitlab.com/IvanSanchez/Leaflet.GridLayer.GoogleMutant)
- [vis.gl React Google Maps](https://visgl.github.io/react-google-maps/docs)
- [OpenLayers Google source](https://openlayers.org/en/latest/examples/google.html)
- [Map Tiles API billing](https://developers.google.com/maps/documentation/tile/usage-and-billing)
- [My Maps embedding requires a public map](https://support.google.com/mymaps/answer/3109452?hl=en)
- [My Maps import limits](https://support.google.com/mymaps/answer/3024836?hl=en)
- [Older leaflet-google plugin](https://github.com/Raruto/leaflet-google)
- [Google Maps URLs require no API key](https://developers.google.com/maps/documentation/urls/get-started)
