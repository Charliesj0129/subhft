#!/usr/bin/env python3
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import os
import time
from datetime import datetime
import ssl

# Arxiv API endpoint
BASE_URL = 'http://export.arxiv.org/api/query'

# Search criteria
KEYWORDS = ['Hawkes processes', 'propagator model']
CATEGORIES = ['q-fin.MF', 'q-fin.TR']
YEAR_START = 2021
OUTPUT_DIR = "research/arxiv_papers"

# SSL context for legacy systems if needed
ssl._create_default_https_context = ssl._create_unverified_context

def fetch_arxiv(search_query, start=0, max_results=50):
    url = f"{BASE_URL}?search_query={search_query}&start={start}&max_results={max_results}&sortBy=submittedDate&sortOrder=descending"
    print(f"Querying: {url}")
    with urllib.request.urlopen(url) as response:
        return response.read()

def parse_entry(entry):
    ns = {'atom': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
    
    id_url = entry.find('atom:id', ns).text
    paper_id = id_url.split('/abs/')[-1]
    title = entry.find('atom:title', ns).text.strip().replace('\n', ' ')
    summary = entry.find('atom:summary', ns).text.strip().replace('\n', ' ')
    published = entry.find('atom:published', ns).text
    published_year = int(published.split('-')[0])
    
    # Get PDF link
    pdf_link = None
    for link in entry.findall('atom:link', ns):
        if link.attrib.get('title') == 'pdf':
            pdf_link = link.attrib.get('href')
    
    # Get categories
    categories = [c.attrib.get('term') for c in entry.findall('atom:category', ns)]
    
    return {
        'id': paper_id,
        'title': title,
        'summary': summary,
        'published': published,
        'year': published_year,
        'pdf_link': pdf_link,
        'categories': categories
    }

def main():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    # Construct query: (abs:"Hawkes processes" OR abs:"propagator model") AND (cat:q-fin.MF OR cat:q-fin.TR)
    # Arxiv query syntax needs careful encoding
    
    # Keyword part: (all:Hawkes+processes+OR+all:propagator+model)
    # Note: 'abs' (abstract) is better but 'all' covers title too.
    kw_query = "%28" + "+OR+".join([f"all:%22{k.replace(' ', '+')}%22" for k in KEYWORDS]) + "%29"
    
    # Category part: (cat:q-fin.MF+OR+cat:q-fin.TR)
    cat_query = "%28" + "+OR+".join([f"cat:{c}" for c in CATEGORIES]) + "%29"
    
    full_query = f"{kw_query}+AND+{cat_query}"
    
    print(f"Starting research on: {KEYWORDS} in {CATEGORIES} since {YEAR_START}")
    
    all_papers = []
    
    # Fetch results
    data = fetch_arxiv(full_query, max_results=50) # Limit to 50 for now
    root = ET.fromstring(data)
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    
    entries = root.findall('atom:entry', ns)
    print(f"Found {len(entries)} candidate papers.")
    
    downloaded_count = 0
    report_lines = ["# Research Report: HFT Hawkes & Propagator Models\n"]
    
    for entry in entries:
        paper = parse_entry(entry)
        
        # Filter by year
        if paper['year'] < YEAR_START:
            continue
            
        print(f"Processing: [{paper['published'][:10]}] {paper['title']}")
        
        # Download PDF
        if paper['pdf_link']:
            filename = f"{paper['id']}_{paper['title'][:30].replace(' ', '_').replace('/', '-')}.pdf"
            filepath = os.path.join(OUTPUT_DIR, filename)
            
            if not os.path.exists(filepath):
                print(f"  Downloading PDF...")
                try:
                    urllib.request.urlretrieve(paper['pdf_link'], filepath)
                    downloaded_count += 1
                    time.sleep(1) # Be nice to Arxiv
                except Exception as e:
                    print(f"  Failed to download: {e}")
            else:
                print(f"  Already exists.")
                
            report_lines.append(f"## {paper['title']}")
            report_lines.append(f"- **Date**: {paper['published'][:10]}")
            report_lines.append(f"- **ID**: [{paper['id']}]({paper['id']})")
            report_lines.append(f"- **PDF**: [Local](./{filename})")
            report_lines.append(f"- **Summary**: {paper['summary'][:300]}...\n")
            
    # Write report
    with open(f"{OUTPUT_DIR}/README.md", "w") as f:
        f.writelines("\n".join(report_lines))
        
    print(f"\nResearch Complete. Downloaded {downloaded_count} new papers to {OUTPUT_DIR}")

if __name__ == "__main__":
    main()
