
import arxiv
import os
import time

CATEGORIES = {
    "stat_arb": [
        "statistical arbitrage", 
        "pairs trading", 
        "cointegration trading",
        "mean reversion trading"
    ],
    "microstructure": [
        "market microstructure", 
        "limit order book dynamics", 
        "order flow toxicity",
        "price impact model"
    ],
    "futures_arb": [
        "futures arbitrage", 
        "basis trading", 
        "spot-futures parity",
        "cash-and-carry arbitrage"
    ],
    "rl_trading": [
        "reinforcement learning trading", 
        "deep reinforcement learning finance", 
        "agent based market simulation"
    ]
}

MAX_PAPERS_TOTAL = 200
MAX_PER_CAT = 50  # Tries to balance deployment

def main():
    client = arxiv.Client(
        page_size=100,
        delay_seconds=3.0,
        num_retries=3
    )

    downloaded_count = 0
    seen_ids = set()

    for cat_name, keywords in CATEGORIES.items():
        print(f"\n--- Processing Category: {cat_name} ---")
        target_dir = f"research/knowledge/papers/{cat_name}"
        os.makedirs(target_dir, exist_ok=True)
        
        # Construct query
        # "search1" OR "search2" ...
        query_str = " OR ".join([f'"{k}"' for k in keywords])
        
        search = arxiv.Search(
            query=query_str,
            max_results=MAX_PER_CAT,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending
        )

        for result in client.results(search):
            if result.entry_id in seen_ids:
                continue
                
            title = result.title.replace("/", "-")
            # Limit filename length
            filename = f"{result.published.year}_{title[:60]}.pdf"
            filepath = os.path.join(target_dir, filename)
            
            if os.path.exists(filepath):
                print(f"Skipping existing: {filename}")
                continue
                
            print(f"Downloading: {result.title} ({result.published.year})")
            try:
                result.download_pdf(dirpath=target_dir, filename=filename)
                seen_ids.add(result.entry_id)
                downloaded_count += 1
                time.sleep(1) # Be polite
            except Exception as e:
                print(f"Failed to download {result.title}: {e}")

            if downloaded_count >= MAX_PAPERS_TOTAL:
                print("Global limit reached.")
                return

    print(f"\nDone. Downloaded {downloaded_count} papers.")

if __name__ == "__main__":
    main()
