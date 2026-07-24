# Dunelm Stock Monitor

Monitors one Dunelm product and sends a Discord alert only when it changes from out of stock to purchasable.

Product: **12000 4 in 1 Portable Air Cooler and Heater**

## Setup

1. Create a public GitHub repository.
2. Upload `monitor.py`, `config.json`, `state.json`, and this README.
3. Create `.github/workflows/monitor.yml` in GitHub and paste the supplied workflow.
4. Create a Discord webhook and save it in repository **Settings → Secrets and variables → Actions** as `DISCORD_WEBHOOK_URL`.
5. Run the workflow once with the test checkbox ticked.
6. Run it again without the checkbox to establish the baseline.

The script deliberately reports an error when the page loads but no trustworthy stock signal can be found. This prevents false restock alerts if Dunelm changes its page.
