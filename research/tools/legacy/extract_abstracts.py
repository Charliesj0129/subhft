
import os
import glob
from pypdf import PdfReader

TARGET_DIR = "research/knowledge/papers"
OUTPUT_FILE = "research/knowledge/reports/paper_abstracts.txt"

def extract_text_from_pdf(pdf_path, max_pages=2):
    try:
        reader = PdfReader(pdf_path)
        text = []
        # Only read first few pages where Abstract/Intro usually reside
        limit = min(len(reader.pages), max_pages)
        for i in range(limit):
            page_text = reader.pages[i].extract_text()
            if page_text:
                text.append(page_text)
        return "\n".join(text)
    except Exception as e:
        print(f"Error reading {pdf_path}: {e}")
        return ""

def clean_text(text):
    # Simple cleanup to make it readable
    # Join hyphenated words, remove excessive newlines
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line: continue
        cleaned.append(line)
    return " ".join(cleaned)

def main():
    pdf_files = glob.glob(f"{TARGET_DIR}/**/*.pdf", recursive=True)
    print(f"Found {len(pdf_files)} PDFs.")

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        for i, pdf_file in enumerate(pdf_files):
            print(f"[{i+1}/{len(pdf_files)}] Processing {pdf_file}...")
            raw_text = extract_text_from_pdf(pdf_file)
            
            # Simple heuristic to extract Abstract
            # We look for "Abstract" and then grab the next 2000 chars
            clean = clean_text(raw_text)
            
            # Identify Category
            category = os.path.basename(os.path.dirname(pdf_file))
            filename = os.path.basename(pdf_file)
            
            f.write(f"--- PAPER START ---\n")
            f.write(f"Category: {category}\n")
            f.write(f"File: {filename}\n")
            
            # Heuristic extraction
            if "abstract" in clean.lower():
                # Find start of abstract
                start_idx = clean.lower().find("abstract")
                # Take next 3000 chars (Abstract + Intro)
                extract = clean[start_idx:start_idx+3000]
                f.write(f"Content_Extract: {extract}...\n")
            else:
                # Fallback: First 2000 chars
                f.write(f"Content_Extract: {clean[:2000]}...\n")
                
            f.write(f"--- PAPER END ---\n\n")

    print(f"Extraction complete. Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
