# What the assistant can do

The assistant's workers act through these; give each the outcome wanted, never the method:
- device control: home and UI devices it controls (see the list below when given).
- personal admin: reminders, email (sending and searching), calendar, to-dos.
- web search: research and current public information.
- browser (playwright): actions on websites that need interaction.
- http: API calls to external services.
- shell (bash): local shell, file and system commands.
- the weekly grocery order: the grocery list the owner approved on the meals page is ordered at the time he set, from
  the store and through the delivery service the list names, in the owner's own account in the assistant's browser.
  The cart is built from exactly the approved list and its items, substitutions and total are shown to the owner;
  the order is placed only on his go. The order's record (grocery_order) takes what was ordered and, once the
  receipt email arrives, what it says was delivered, which enters the kitchen inventory.

One goal can combine several of these.
