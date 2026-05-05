---
name: cnn-site
description: Browser-automation guidelines for cnn.com — first-load scroll, overlay handling, headline-extraction layout. Use when the playwright agent is operating on cnn.com.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
  auto_inject_when:
    task_keywords: ["cnn.com", "cnn"]
---

# CNN Browser Automation Guidelines

## First action after page load
- Immediately scroll down ~250px after navigating to the page. This clears the top ad banner and sticky header from the viewport, bringing the actual headline content into view.

## Overlays and modals
- CNN frequently shows a 'Live TV' promo overlay on first load. Look for an X button or 'close' control in the top-right area to dismiss it.
- A 'DRM System Not Supported' panel may appear over the video tile area. This is embedded in the video player and cannot be dismissed — work around it by focusing on the non-video content areas.
- Cookie consent banners may appear at the bottom. Dismiss with 'Accept' or 'I agree' if they block interaction.

## Content structure
- The top-stories/headlines area is usually a prominent multi-column layout below the header and live-updates ticker strip.
- CNN has multiple content sections. The lead headline is typically center-column. Supporting headlines may be in left/right columns.
- Prefer extracting headlines from anchor text rather than image alt text.
