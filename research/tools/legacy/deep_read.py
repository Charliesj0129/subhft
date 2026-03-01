
from pypdf import PdfReader
import os

TARGET_PAPERS = [
    "research/knowledge/papers/microstructure/2026_A unified theory of order flow, market impact, and volatilit.pdf",
    "research/knowledge/papers/microstructure/2026_Directional Liquidity and Geometric Shear in Pregeometric Or.pdf",
    "research/knowledge/papers/microstructure/2026_WebCryptoAgent: Agentic Crypto Trading with Web Informatics.pdf",
]

def extract_methodology(pdf_path):
    if not os.path.exists(pdf_path):
        return f"MISSING FILE: {pdf_path}"
    
    try:
        reader = PdfReader(pdf_path)
        text = []
        # Typically methodology is pages 2-8
        start_page = 2
        end_page = min(len(reader.pages), 12) 
        
        text.append(f"--- EXTRACT FROM: {os.path.basename(pdf_path)} ---")
        for i in range(start_page, end_page):
            page_content = reader.pages[i].extract_text()
            if page_content:
                text.append(f"\n[PAGE {i+1}]\n{page_content}")
        return "\n".join(text)
    except Exception as e:
        return f"ERROR reading {pdf_path}: {str(e)}"

def main():
    output = []
    for paper in TARGET_PAPERS:
        output.append(extract_methodology(paper))
        output.append("\n" + "=" * 50 + "\n")

    out_path = "research/knowledge/reports/deep_read_content.txt"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(output))

    print("Deep read extraction complete.")

if __name__ == "__main__":
    main()
