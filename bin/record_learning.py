import argparse
import os
import datetime

BRAIN_PATH = "/home/charlie/hft_platform/brain/knowledge_base"
LOGS_PATH = "/home/charlie/hft_platform/brain/logs"

CATEGORIES = {
    "rust_patterns": os.path.join(BRAIN_PATH, "rust_patterns.md"),
    "infra_quirks": os.path.join(BRAIN_PATH, "infra_quirks.md"),
    "decisions": os.path.join(LOGS_PATH, "decisions.log"),
    "general": os.path.join(BRAIN_PATH, "general_knowledge.md")
}

def record(args):
    target_file = CATEGORIES.get(args.category)
    if not target_file:
        print(f"Unknown category: {args.category}. Valid: {list(CATEGORIES.keys())}")
        return

    # Create dir if not exists
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    
    timestamp = datetime.datetime.now().isoformat()
    
    entry = f"\n\n## {args.title}\n"
    entry += f"**Date**: {timestamp}\n"
    if args.tags:
        entry += f"**Tags**: {args.tags}\n"
    entry += f"\n{args.content}\n"
    
    with open(target_file, "a") as f:
        f.write(entry)
        
    print(f"Recorded new entry in {target_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", required=True, choices=list(CATEGORIES.keys()))
    parser.add_argument("--title", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--tags", help="Comma separated tags")
    
    args = parser.parse_args()
    record(args)
