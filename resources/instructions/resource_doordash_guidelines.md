# DoorDash Browser Automation Guidelines

## Modals and overlays
- DoorDash frequently shows a modal overlay after landing. 
- Look for signs of a modal and dismiss them, do not sign up for promotional materials or deals and do not concent to anyone emailing or texting you.
- Some signs of modals:
 uses `position: fixed` covering the viewport with "Agree" and "Not now" (or "Close") buttons. Dismiss unwanted modals if you can detect the close button.  Or call `web_page_coords` with question
 Ordering food is also a modal, so do not dismiss these.
- Item-detail modals open in a bottom sheet when adding to cart. Often you must scroll down inside the modal to see all required customization options. Use `web_scroll` (not the scrollbar). Make sure modal is active otherwise scrolling may not work. You may need to click inside the modal or at least hover your mouse over it.
- Inside modal it is often much faster to pick options using snapshot refs, if you can do this without visual tools, do it. However, if you are stuck, then switch to visuals.  
- When ordering food items you are often asked to customize them.  There is usually something that tells you how many required customizations there are.
- Once the required customizations are done, the bottom button will turn into SAVE or equivalent.  This SAVE button acts also as continue to next step that may allow you to 
- do more customizations. For example you might first customize the sandwich, then the drink, then the fries.

## Navigation
- Clicking a restaurant name may open it **in a new tab**. This is handled automatically — the click tools detect a new tab and switch to it. If the page content looks unexpected after clicking, check the snapshot URL.
- The address/location field is in the top navigation bar. Use `web_page_coords` with question `"delivery address input or change address"` to find it.

For text boxes always prefer the `web_fill_xy` tool. This writes your text into the coords xy filling the text box and pushing enter for you to submit.

## Delivery address
- The user's home address is available in the system prompt (resource_user_location). Use it when the task says "home" or does not specify a delivery address. This should already be set. No need to check until something tells you that this information is missing.
- In general this address should already be filled in and you don't need to do anything about this.

## Search and filtering
- The search bar is at the top. Use `web_fill_xy` to type and submit a search query.
- If you know the restaurant you are ordering always use search to directly go to that restauraunt.

- Filter buttons (e.g. "Breakfast", "Burgers") are horizontal pills below the search bar — use `web_click_ref_snapshot` if refs are available, otherwise `web_page_coords`.
- You can use also the search to filter to specific type of restaurant.


## Cart and checkout
- "Add to cart" / "Add to bag" buttons are inside item-detail modals. Required options (size, protein, etc.) must be selected before the button becomes enabled.
- You may go to the cart to verify the order.
- Once the final order is ready notify the user.

## Common pitfalls
- DoorDash is a heavy React SPA — refs in snapshots can become stale quickly. Prefer `web_page_coords` for visual targets when refs are unreliable. Your first try should be refs, but you should quickly realize if something is not working and use visual tools. 
