
import os
import glob
from pypdf import PdfReader
import re

TARGET_DIR = "research/knowledge/papers"
OUTPUT_FILE = "research/knowledge/summaries/comprehensive_summaries.md"

def clean_text(text):
    """Cleans extracted text for markdown."""
    text = re.sub(r'-\n', '', text)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_section(text, start_patterns, end_patterns, max_chars=2500):
    """Generic fuzzy section extractor."""
    text_lower = text.lower()
    start_idx = -1
    for p in start_patterns:
        idx = text_lower.find(p)
        if idx != -1:
            start_idx = idx + len(p)
            break
            
    if start_idx == -1:
        return None

    # Find end
    end_idx = -1
    search_text = text_lower[start_idx:]
    for p in end_patterns:
        idx = search_text.find(p)
        if idx != -1 and idx < max_chars:
             end_idx = start_idx + idx
             break

    if end_idx == -1:
        end_idx = min(start_idx + max_chars, len(text))
        
    return clean_text(text[start_idx:end_idx])

def extract_detailed_content(pdf_path):
    try:
        reader = PdfReader(pdf_path)
        num_pages = len(reader.pages)
        
        # Read First 5 Pages (Abstract, Intro, Method usually here)
        early_text = ""
        for i in range(min(5, num_pages)):
            early_text += reader.pages[i].extract_text() + "\n"
            
        # Read Middle Pages (Methodology check)
        mid_text = ""
        if num_pages > 5:
             # Sample a few pages from the middle to check for methodology
             mid_start = num_pages // 3
             for i in range(mid_start, min(mid_start+3, num_pages)):
                  mid_text += reader.pages[i].extract_text() + "\n"

        # Read Last 3 Pages (Conclusion / Discussion)
        end_text = ""
        if num_pages > 3:
            for i in range(max(0, num_pages-3), num_pages):
                end_text += reader.pages[i].extract_text() + "\n"
        else:
            end_text = early_text

        full_text_sample = early_text + "\n" + mid_text + "\n" + end_text

        # 1. Abstract
        abstract = extract_section(early_text, ["abstract"], ["1. introduction", "introduction", "1 introduction"]) or "Abstract not found."

        # 2. Context (Introduction)
        context = "N/A"
        if "in this paper" in early_text.lower():
             idx = early_text.lower().find("in this paper")
             context = clean_text(early_text[idx:idx+800]) + "..."
        elif "we propose" in early_text.lower():
             idx = early_text.lower().find("we propose")
             context = clean_text(early_text[idx:idx+800]) + "..."
        
        # 3. Methodology Candidates
        # Look for section headers like "3. Methodology", "2. Model", "The Model"
        methodology = "N/A"
        method_headers = ["methodology", "proposed method", "the model", "system model", "problem formulation"]
        
        for header in method_headers:
            # Try to find header in early or mid text
            section = extract_section(full_text_sample, [header], ["result", "experiment", "evaluation", "conclusion"], max_chars=1500)
            if section and len(section) > 100:
                methodology = section + "..."
                break

        # 4. Results (Conclusion)
        results = extract_section(end_text, ["conclusion", "conclusions", "discussion"], ["references", "acknowledgments"]) or "Conclusion not found."

        return {
            "filename": os.path.basename(pdf_path),
            "category": os.path.basename(os.path.dirname(pdf_path)),
            "abstract": abstract,
            "context": context,
            "methodology": methodology,
            "results": results,
            "year": "2026" if "2026" in pdf_path else "2025" # Simple heuristic
        }
    except Exception as e:
        return {"error": str(e), "filename": os.path.basename(pdf_path)}

def main():
    print("Starting Deep Template Extraction...")
    pdf_files = glob.glob(f"{TARGET_DIR}/**/*.pdf", recursive=True)
    pdf_files = sorted(pdf_files)

    markdown_content = []
    
    for pdf in pdf_files:
        data = extract_detailed_content(pdf)
        if "error" in data: continue

        title = data['filename'].replace('.pdf','').replace('_',' ')
        
        # --- TEMPLATE RENDERING ---
        entry = f"""
# {title}

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)
• **標題**： {title}
• **檔案**： `{data['filename']}`
• **年份**： {data['year']}
• **類別**： `{data['category']}`
• **閱讀狀態**： 🔴 待讀 (Auto-Generated)

---

### 🎯 研究背景與目標 (Context & Objectives)
• **Research Gap & Purpose (Extracted)**：
> {data['context']}

• **Abstract Reference**:
> {data['abstract'][:1000]}...

---

### 🛠 研究方法論 (Methodology - Extracted Candidate)
• **Model / Framework**:
> {data['methodology']}

---

### 📊 結果與討論 (Results & Discussion)
• **Conclusion / Key Findings**:
> {data['results']}

---

### 🧠 深度評析 (Synthesis & Critique)
> *[Pending Human Review]*

---

### 🚀 行動清單 (Action Items)
- [ ] Verify methodology relevance.
- [ ] Check if this paper belongs to the "Deep Read" list?

---
"""
        markdown_content.append(entry)

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write("\n".join(markdown_content))
    print(f"Deep Template Summary Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
