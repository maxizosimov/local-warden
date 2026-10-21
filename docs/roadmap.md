# Roadmap

From a demo anyone can run to something worth $5 a month.

1. **Real payments.** Implement `vault.py` with single-use virtual cards
   (Lithic, Stripe Issuing, Privacy.com). Each approval maps onto the card's
   merchant lock and spending cap, and the issuer's real-time authorization
   webhook lets policy approve or decline each charge, so even a compromised
   agent can't overspend. Move users from handoff to these cards, never to
   their real card or bank login.
2. **Approvals off the machine.** Sign approvals on your phone and store the
   log's latest hash outside the machine, so a local agent can't approve its
   own purchases or rewrite history.
3. **More verified prices.** Read prices that only appear after JavaScript
   runs, with a local headless browser.
4. **Connected stores.** Official APIs where they exist, otherwise checkout in
   the user's own logged-in browser.
5. **Order tracking.** Read order and shipping emails locally with the local
   model instead of scraping store sites.
6. **Packaging.** A desktop app with approvals pushed to your phone. Charge
   for the hosted relay (push, encrypted sync), not the local core.

Done: handoff mode and track record, MCP server, products from any website,
local model picks the best match.
