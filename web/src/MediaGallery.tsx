import { useState } from "react";
import { CircleAlert, ExternalLink, Image as ImageIcon } from "lucide-react";
import type { MediaAttachment } from "./api";

export function localMediaUrl(item: MediaAttachment): string | null {
  try {
    const url = new URL(item.url, window.location.origin);
    return url.origin === window.location.origin &&
      url.pathname.startsWith("/api/files/")
      ? url.href
      : null;
  } catch {
    return null;
  }
}

function MediaItem({ item }: { item: MediaAttachment }) {
  const [failed, setFailed] = useState(false);
  const url = localMediaUrl(item);
  if (!url)
    return (
      <div className="media-unavailable">
        <CircleAlert size={15} />
        This attachment has no local media file.
      </div>
    );
  const isVideo = item.kind === "video" || item.mime?.startsWith("video/");
  const isImage = item.kind === "image" || item.mime?.startsWith("image/");
  return (
    <figure className={`media-item ${isVideo ? "media-video" : ""}`}>
      {failed || (!isImage && !isVideo) ? (
        <a
          className="media-unavailable"
          href={url}
          target="_blank"
          rel="noreferrer"
        >
          <CircleAlert size={16} />
          {failed
            ? "Preview unavailable. Open original media"
            : "Open media attachment"}
          <ExternalLink size={13} />
        </a>
      ) : isVideo ? (
        <video
          controls
          preload="none"
          playsInline
          src={url}
          onError={() => setFailed(true)}
          aria-label={item.caption || "Attached video"}
        />
      ) : (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          aria-label={
            item.caption ? `Open image: ${item.caption}` : "Open original image"
          }
        >
          <img
            src={url}
            loading="lazy"
            decoding="async"
            alt={item.caption || "Image attached to this record"}
            onError={() => setFailed(true)}
          />
        </a>
      )}
      {item.caption && <figcaption>{item.caption}</figcaption>}
    </figure>
  );
}

export default function MediaGallery({
  media,
  compact = false,
}: {
  media?: MediaAttachment[];
  compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!media?.length) return null;
  const visible = compact && !expanded ? media.slice(0, 2) : media;
  return (
    <section
      className={`media-gallery ${compact ? "compact" : ""}`}
      aria-label="Attached images and videos"
    >
      <div className="media-grid">
        {visible.map((item, index) => (
          <MediaItem key={`${item.file_id}-${index}`} item={item} />
        ))}
      </div>
      {compact && media.length > 2 && !expanded && (
        <button className="media-more" onClick={() => setExpanded(true)}>
          <ImageIcon size={13} />
          Show {media.length - 2} more attachments
        </button>
      )}
    </section>
  );
}
