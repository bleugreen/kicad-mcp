"""Datasheet lookup via DuckDuckGo search."""

import json
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, Tuple
from ddgs import DDGS

# Known manufacturer domains
MFG_DOMAINS = {
    "TEXAS INSTRUMENTS": "ti.com",
    "TI": "ti.com",
    "STMICROELECTRONICS": "st.com",
    "ST": "st.com",
    "MICROCHIP": "microchip.com",
    "NXP": "nxp.com",
    "INFINEON": "infineon.com",
    "ANALOG DEVICES": "analog.com",
    "ADI": "analog.com",
    "MAXIM": "maximintegrated.com",
    "ON SEMICONDUCTOR": "onsemi.com",
    "ONSEMI": "onsemi.com",
}

DISTRIBUTORS = ["digikey.com", "mouser.com", "farnell.com", "arrow.com"]


class DatasheetCache:
    """Cache for datasheet URLs."""

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize datasheet cache.

        Args:
            cache_dir: Directory for cache file (defaults to ~/.cache/kicad_mcp)
        """
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "kicad_mcp"
        cache_dir.mkdir(parents=True, exist_ok=True)

        self.cache_file = cache_dir / "datasheets.json"
        self.cache: Dict[str, str] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load cache from disk."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r') as f:
                    self.cache = json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load datasheet cache: {e}")
                self.cache = {}

    def _save_cache(self) -> None:
        """Save cache to disk."""
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to save datasheet cache: {e}")

    def _make_key(self, manufacturer: str, part_number: str) -> str:
        """Create cache key from manufacturer and part number."""
        return f"{manufacturer.upper()}::{part_number.upper()}"

    def get(self, manufacturer: str, part_number: str) -> Optional[str]:
        """Get cached datasheet URL.

        Args:
            manufacturer: Component manufacturer
            part_number: Component part number

        Returns:
            Cached URL or None if not found
        """
        key = self._make_key(manufacturer, part_number)
        return self.cache.get(key)

    def put(self, manufacturer: str, part_number: str, url: str) -> None:
        """Store datasheet URL in cache.

        Args:
            manufacturer: Component manufacturer
            part_number: Component part number
            url: Datasheet URL
        """
        key = self._make_key(manufacturer, part_number)
        self.cache[key] = url
        self._save_cache()


class DatasheetFinder:
    """Find datasheets via DuckDuckGo search."""

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize datasheet finder.

        Args:
            cache_dir: Directory for cache file
        """
        self.cache = DatasheetCache(cache_dir)

    def _ddg_search_urls(self, query: str, max_results: int = 10) -> list[str]:
        """Search DuckDuckGo and return result URLs.

        Args:
            query: Search query
            max_results: Maximum number of results to return

        Returns:
            List of result URLs
        """
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
                urls = [r['href'] for r in results if 'href' in r]
                return urls
        except Exception as e:
            print(f"Search failed: {e}")
            return []

    def _canon_domain(self, mfg: str) -> Optional[str]:
        """Get canonical domain for manufacturer.

        Args:
            mfg: Manufacturer name

        Returns:
            Canonical domain or None
        """
        return MFG_DOMAINS.get(mfg.strip().upper())

    def _is_pdf(self, url: str) -> bool:
        """Check if URL points to a PDF.

        Args:
            url: URL to check

        Returns:
            True if URL appears to be a PDF
        """
        return ".pdf" in url.lower().split("?", 1)[0]

    def _rank_url(self, url: str, part_number: str, mfg_domain: Optional[str]) -> Tuple[int, int]:
        """Rank URL by relevance.

        Args:
            url: URL to rank
            part_number: Component part number
            mfg_domain: Expected manufacturer domain

        Returns:
            (domain_rank, name_rank) tuple - lower is better
        """
        host = urlparse(url).netloc.lower()
        pn_norm = part_number.upper().replace(" ", "").replace("-", "")
        fname = url.split("/")[-1].upper().replace("-", "").replace("_", "")

        # Rank by domain
        if mfg_domain and mfg_domain in host:
            domain_rank = 0  # Manufacturer site is best
        elif any(d in host for d in DISTRIBUTORS):
            domain_rank = 1  # Distributor sites are good
        else:
            domain_rank = 2  # Other sites

        # Rank by filename match
        name_rank = 0 if pn_norm in fname else 1

        return (domain_rank, name_rank)

    def find_datasheet(self, manufacturer: str, part_number: str, use_cache: bool = True) -> Optional[str]:
        """Find datasheet URL for a component.

        Args:
            manufacturer: Component manufacturer
            part_number: Component part number
            use_cache: Whether to use cached results

        Returns:
            Datasheet URL or None if not found
        """
        # Check cache first
        if use_cache:
            cached = self.cache.get(manufacturer, part_number)
            if cached:
                return cached

        pn = part_number.strip()
        mfg_domain = self._canon_domain(manufacturer)

        # Build search queries with filetype:pdf to get direct PDF links
        # Use quotes for exact matching
        queries = []

        # Try with manufacturer domain if known
        if mfg_domain:
            queries.append(f'"{manufacturer}" "{pn}" datasheet filetype:pdf site:{mfg_domain}')

        # Try general searches
        queries.append(f'"{manufacturer}" "{pn}" datasheet filetype:pdf')
        queries.append(f'"{pn}" datasheet filetype:pdf')

        urls = []
        for query in queries:
            results = self._ddg_search_urls(query, max_results=15)
            # Filter to only PDF URLs (filetype:pdf doesn't always guarantee PDFs)
            pdf_urls = [u for u in results if self._is_pdf(u)]
            if pdf_urls:
                urls.extend(pdf_urls)
                break  # Found PDFs, no need to try more queries

        if not urls:
            return None

        # Sort by relevance and take best match
        urls.sort(key=lambda u: self._rank_url(u, pn, mfg_domain))
        best_url = urls[0]

        # Cache the result
        self.cache.put(manufacturer, part_number, best_url)

        return best_url
