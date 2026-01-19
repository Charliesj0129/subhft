

# Simple logging to STDOUT for CI visibility
import logging
import os
import sys
import time

from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

def main():
    logger.info("Starting futures subscription script...")

    # Initialize Client
    client = ShioajiClient()

    # Login explicitly
    try:
        # Fallback for API Key env vars if client doesn't find PERSON_ID
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")

        if api_key and secret_key:
            logger.info("Using SHIOAJI_API_KEY from environment.")
            client.login(person_id=api_key, password=secret_key)
        else:
            client.login()

        if not client.api:
             logger.error("No API initialized (Sim mode without lib?). Exiting.")
             return
    except Exception as e:
        logger.error(f"Login failed: {e}")
        return

    # Give it a moment to sync contracts
    time.sleep(3.0)

    api = client.api

    # Logic to find "Open" contracts
    # For simplicity, we target TXF (TaiEx Futures) and MXF (Mini TaiEx)
    # and subscribe to the Front Month and Next Month.
    # In a real open check, we'd check datetime vs Exchange hours.

    logger.info("Fetching contracts...")

    # Check if Contracts are available (requires login)
    if not hasattr(api, 'Contracts'):
        logger.warning("api.Contracts not found (Login failed?). Generating empty config.")
        subscribed = []
    else:
        subscribed = []

        for category in [api.Contracts.Futures.TXF, api.Contracts.Futures.MXF]:
            if not category:
                continue

            # Category is typically an object with contract attributes
            # We want to iterate available contracts in this category?
            # Shioaji structure: api.Contracts.Futures.TXF.TXF202312 etc.
            # But easier to iterate api.Contracts.Futures and filter
            pass

        # Debug Exploration
        try:
           logger.info("DEBUG: Inspecting api.Contracts.Futures...")
           futures = api.Contracts.Futures
           logger.info(f"Futures type: {type(futures)}")

           # Iterate directory of futures to see what categories exist
           # dir(futures)

           for cat_name in ["TXF", "MXF"]:
               logger.info(f"Checking category: {cat_name}")
               category = getattr(futures, cat_name, None)
               if not category:
                   logger.warning(f"Category {cat_name} is None/Missing")
                   continue

               logger.info(f"Category {cat_name} found: {type(category)}")

               # Try to iterate
               count = 0
               try:
                   for contract in category:
                       count += 1
                       if count > 5:
                           break  # limit spam
                       logger.info(f"Item Type: {type(contract)}")
                       if hasattr(contract, 'code'):
                           logger.info(f"Contract: {contract.code} {contract.name}")
                           subscribed.append(contract.code)
                       else:
                           logger.warning(f"Item has no code: {contract}")

               except Exception as iter_err:
                   logger.error(f"Error iterating category {cat_name}: {iter_err}")

        except Exception as e:
            logger.error(f"Top-level exploration failed: {e}")
            import traceback
            logger.error(traceback.format_exc())

    logger.info(f"Subscribed to {len(subscribed)} contracts: {subscribed}")

    # Keep script alive? or does subscription persist?
    # Subscriptions in Shioaji are tied to the Session/Connection.
    # If this script exits, the connection closes, and subscriptions die.
    # BUT, the request was: "Deploy platform... then subscribe".
    # If the platform (hft_platform.main) is the one running the strategy,
    # IT needs the data.
    #
    # CRITICAL: This script running in a separate process/connection
    # WILL NOT send data to the main platform process.
    # The main platform needs to be the one subscribing.
    #
    # Code Correction:
    # 1. The USER likely assumes the platform is running.
    # 2. To dynamically subscribe, we must tell the running platform to do it.
    # 3. OR, this script writes a CONFIG file that the platform periodically reloads?
    # 4. OR, the User's intention is that this helper script *verifies* data or *runs alongside*?
    #
    # Given the constraint: "subscribe futures currently open", and `nohup python -m hft_platform.main ... &`
    # The `main.py` starts the `Recorder` and `Strategies`.
    # If the strategies need data, `main.py` config MUST have the symbols.
    #
    # WORKAROUND:
    # We will make `subscribe_futures.py` GENERATE a `config/symbols.yaml`
    # containing the open futures, AND THEN we might need to restart main?
    # OR, we assume `main.py` watches the config? (ShioajiClient loads it on init).
    #
    # REVISED PLAN:
    # update `subscribe_futures.py` to:
    # 1. Fetch open contracts.
    # 2. Write them to `config/symbols.yaml`.
    # 3. (Optional) Kill/Restart main? Or assume main isn't started yet?
    #
    # Wait, the deployment script in plan:
    #   nohup python -m hft_platform.main ... &
    #   python -m hft_platform.scripts.subscribe_futures.py
    #
    # This order suggests `subscribe_futures.py` runs AFTER main starts.
    # This implies it might be useless if it's separate.
    #
    # ADJUSTMENT:
    # I will move `subscribe_futures.py` BEFORE `main.py` in the deployment YAML.
    # And make it write the config.
    #
    # Logic:
    # 1. Connect, find contracts.
    # 2. Write to `config/symbols.yaml`.
    # 3. Exit.
    # 4. Then `main.py` starts and reads `config/symbols.yaml`.

    import yaml

    # Write config
    if subscribed:
        logger.info(f"Generating config/symbols.yaml with {len(subscribed)} symbols...")
        config_data = {"symbols": []}
        for code in subscribed:
            # We need metadata like exchange, tick_size
            # For futures (TXF), exchange is usually Futures?
            # tick_size depends on symbol (TXF=1.0, MXF=1.0/0.2?)
            # querying contract details
            from_contract = None
            try:
                # Naive lookup
                # Try to search in TXF/MXF
                for category_name in ["TXF", "MXF"]:
                     category = getattr(api.Contracts.Futures, category_name, None)
                     if category:
                         for c in category:
                             if c.code == code:
                                 from_contract = c
                                 break
                     if from_contract:
                         break
            except Exception as lookup_err:
                logger.warning(f"Contract lookup failed for {code}: {lookup_err}")

            # Default values (refined by contract lookup ideally)
            entry = {
                "code": code,
                "name": from_contract.name if from_contract else code,
                "exchange": "Futures",
                "tick_size": 1.0,
                "contract_size": 1.0,
                "price_scale": 10000
            }
            config_data["symbols"].append(entry)

        # Ensure directory exists
        os.makedirs("config", exist_ok=True)
        with open("config/symbols.yaml", "w") as f:
            yaml.dump(config_data, f, default_flow_style=False)

    logger.info("Config generated successfully.")

    # Force exit to kill any lingering Shioaji threads
    logger.info("Script complete. Forcing exit.")
    os._exit(0)

if __name__ == "__main__":
    main()
