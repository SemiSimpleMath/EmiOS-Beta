# BBC Browser Automation Guidelines

## First action after page load
- Immediately scroll down ~250px after navigating to the page. This clears the top navigation bar and any promotional banners from the viewport, bringing the headline content into view.

## Overlays and modals
- BBC may show a cookie consent banner on first visit. Look for 'Accept' or 'Yes, I agree' to dismiss it.
- Occasionally a sign-in prompt or notification permission request appears. Dismiss with 'No thanks' or the X button.

## Content structure
- BBC's homepage has a prominent top story with a large image and headline, followed by a grid of secondary stories.
- Headlines are typically in `h3` elements inside article cards.
- The main news content is below the navigation and any 'breaking news' ticker strip.
- BBC uses a clean layout — once the cookie banner is dismissed, content is usually unobstructed.
- Prefer extracting headlines from anchor text within the main content area, not from sidebar or 'Most Read' sections.
