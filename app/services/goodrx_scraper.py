"""GoodRx price scraper (Function 2 — pharmacy pricing channel).

Constitution: "For every service at every provider, dynamically compares all
pricing channels. Any pricing channel that physically exists and is legally
accessible."

GoodRx has no public API. Their prices ARE visible on their website.
Web scraping publicly displayed prices is legal per hiQ Labs v. LinkedIn (2022).

This scraper:
1. Fetches GoodRx.com pages for common drugs
2. Extracts displayed discount prices
3. Compares against NADAC and other pharmacy channels
4. Records every comparison as structured data feeding F8
5. Documents any barriers (403, CAPTCHA, etc.)
"""

import logging
import re
import uuid
from datetime import datetime, UTC
from typing import Optional

import httpx
from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)

# Top 100 most prescribed drugs in the US (generic names + common NDC prefixes)
# Source: CMS Medicare Part D Prescriber PUF, GoodRx most-searched
TOP_PRESCRIBED_DRUGS = [
    {"name": "atorvastatin", "brand": "Lipitor", "class": "statin"},
    {"name": "lisinopril", "brand": "Zestril", "class": "ace_inhibitor"},
    {"name": "metformin", "brand": "Glucophage", "class": "antidiabetic"},
    {"name": "amlodipine", "brand": "Norvasc", "class": "calcium_channel_blocker"},
    {"name": "metoprolol", "brand": "Lopressor", "class": "beta_blocker"},
    {"name": "omeprazole", "brand": "Prilosec", "class": "ppi"},
    {"name": "simvastatin", "brand": "Zocor", "class": "statin"},
    {"name": "losartan", "brand": "Cozaar", "class": "arb"},
    {"name": "albuterol", "brand": "ProAir", "class": "bronchodilator"},
    {"name": "gabapentin", "brand": "Neurontin", "class": "anticonvulsant"},
    {"name": "hydrochlorothiazide", "brand": "Microzide", "class": "diuretic"},
    {"name": "sertraline", "brand": "Zoloft", "class": "ssri"},
    {"name": "levothyroxine", "brand": "Synthroid", "class": "thyroid"},
    {"name": "acetaminophen-hydrocodone", "brand": "Vicodin", "class": "opioid"},
    {"name": "amoxicillin", "brand": "Amoxil", "class": "antibiotic"},
    {"name": "pantoprazole", "brand": "Protonix", "class": "ppi"},
    {"name": "furosemide", "brand": "Lasix", "class": "diuretic"},
    {"name": "prednisone", "brand": "Deltasone", "class": "corticosteroid"},
    {"name": "escitalopram", "brand": "Lexapro", "class": "ssri"},
    {"name": "rosuvastatin", "brand": "Crestor", "class": "statin"},
    {"name": "tramadol", "brand": "Ultram", "class": "analgesic"},
    {"name": "duloxetine", "brand": "Cymbalta", "class": "snri"},
    {"name": "trazodone", "brand": "Desyrel", "class": "antidepressant"},
    {"name": "montelukast", "brand": "Singulair", "class": "leukotriene_modifier"},
    {"name": "clopidogrel", "brand": "Plavix", "class": "antiplatelet"},
    {"name": "fluticasone", "brand": "Flonase", "class": "corticosteroid"},
    {"name": "meloxicam", "brand": "Mobic", "class": "nsaid"},
    {"name": "pravastatin", "brand": "Pravachol", "class": "statin"},
    {"name": "carvedilol", "brand": "Coreg", "class": "beta_blocker"},
    {"name": "tamsulosin", "brand": "Flomax", "class": "alpha_blocker"},
    {"name": "bupropion", "brand": "Wellbutrin", "class": "antidepressant"},
    {"name": "alprazolam", "brand": "Xanax", "class": "benzodiazepine"},
    {"name": "cyclobenzaprine", "brand": "Flexeril", "class": "muscle_relaxant"},
    {"name": "ibuprofen", "brand": "Advil", "class": "nsaid"},
    {"name": "cephalexin", "brand": "Keflex", "class": "antibiotic"},
    {"name": "azithromycin", "brand": "Zithromax", "class": "antibiotic"},
    {"name": "fluoxetine", "brand": "Prozac", "class": "ssri"},
    {"name": "spironolactone", "brand": "Aldactone", "class": "diuretic"},
    {"name": "potassium-chloride", "brand": "Klor-Con", "class": "supplement"},
    {"name": "doxycycline", "brand": "Vibramycin", "class": "antibiotic"},
    {"name": "ciprofloxacin", "brand": "Cipro", "class": "antibiotic"},
    {"name": "venlafaxine", "brand": "Effexor", "class": "snri"},
    {"name": "ranitidine", "brand": "Zantac", "class": "h2_blocker"},
    {"name": "warfarin", "brand": "Coumadin", "class": "anticoagulant"},
    {"name": "insulin-glargine", "brand": "Lantus", "class": "insulin"},
    {"name": "glipizide", "brand": "Glucotrol", "class": "antidiabetic"},
    {"name": "diazepam", "brand": "Valium", "class": "benzodiazepine"},
    {"name": "clonazepam", "brand": "Klonopin", "class": "benzodiazepine"},
    {"name": "methylphenidate", "brand": "Ritalin", "class": "stimulant"},
    {"name": "amphetamine-dextroamphetamine", "brand": "Adderall", "class": "stimulant"},
    {"name": "oxycodone", "brand": "OxyContin", "class": "opioid"},
    {"name": "morphine", "brand": "MS Contin", "class": "opioid"},
    {"name": "naproxen", "brand": "Aleve", "class": "nsaid"},
    {"name": "cetirizine", "brand": "Zyrtec", "class": "antihistamine"},
    {"name": "loratadine", "brand": "Claritin", "class": "antihistamine"},
    {"name": "ondansetron", "brand": "Zofran", "class": "antiemetic"},
    {"name": "clonidine", "brand": "Catapres", "class": "antihypertensive"},
    {"name": "finasteride", "brand": "Proscar", "class": "5alpha_reductase"},
    {"name": "quetiapine", "brand": "Seroquel", "class": "antipsychotic"},
    {"name": "aripiprazole", "brand": "Abilify", "class": "antipsychotic"},
    {"name": "topiramate", "brand": "Topamax", "class": "anticonvulsant"},
    {"name": "zolpidem", "brand": "Ambien", "class": "sedative"},
    {"name": "propranolol", "brand": "Inderal", "class": "beta_blocker"},
    {"name": "valacyclovir", "brand": "Valtrex", "class": "antiviral"},
    {"name": "lithium", "brand": "Lithobid", "class": "mood_stabilizer"},
    {"name": "methotrexate", "brand": "Trexall", "class": "immunosuppressant"},
    {"name": "hydroxychloroquine", "brand": "Plaquenil", "class": "antimalarial"},
    {"name": "sumatriptan", "brand": "Imitrex", "class": "triptan"},
    {"name": "acyclovir", "brand": "Zovirax", "class": "antiviral"},
    {"name": "pregabalin", "brand": "Lyrica", "class": "anticonvulsant"},
    {"name": "citalopram", "brand": "Celexa", "class": "ssri"},
    {"name": "mirtazapine", "brand": "Remeron", "class": "antidepressant"},
    {"name": "benazepril", "brand": "Lotensin", "class": "ace_inhibitor"},
    {"name": "ramipril", "brand": "Altace", "class": "ace_inhibitor"},
    {"name": "valsartan", "brand": "Diovan", "class": "arb"},
    {"name": "olmesartan", "brand": "Benicar", "class": "arb"},
    {"name": "irbesartan", "brand": "Avapro", "class": "arb"},
    {"name": "diltiazem", "brand": "Cardizem", "class": "calcium_channel_blocker"},
    {"name": "nifedipine", "brand": "Procardia", "class": "calcium_channel_blocker"},
    {"name": "atenolol", "brand": "Tenormin", "class": "beta_blocker"},
    {"name": "bisoprolol", "brand": "Zebeta", "class": "beta_blocker"},
    {"name": "doxazosin", "brand": "Cardura", "class": "alpha_blocker"},
    {"name": "terazosin", "brand": "Hytrin", "class": "alpha_blocker"},
    {"name": "sulfamethoxazole-trimethoprim", "brand": "Bactrim", "class": "antibiotic"},
    {"name": "nitrofurantoin", "brand": "Macrobid", "class": "antibiotic"},
    {"name": "metronidazole", "brand": "Flagyl", "class": "antibiotic"},
    {"name": "clindamycin", "brand": "Cleocin", "class": "antibiotic"},
    {"name": "levofloxacin", "brand": "Levaquin", "class": "antibiotic"},
    {"name": "amoxicillin-clavulanate", "brand": "Augmentin", "class": "antibiotic"},
    {"name": "pioglitazone", "brand": "Actos", "class": "antidiabetic"},
    {"name": "glimepiride", "brand": "Amaryl", "class": "antidiabetic"},
    {"name": "sitagliptin", "brand": "Januvia", "class": "antidiabetic"},
    {"name": "empagliflozin", "brand": "Jardiance", "class": "antidiabetic"},
    {"name": "liraglutide", "brand": "Victoza", "class": "glp1_agonist"},
    {"name": "semaglutide", "brand": "Ozempic", "class": "glp1_agonist"},
    {"name": "tirzepatide", "brand": "Mounjaro", "class": "gip_glp1"},
    {"name": "apixaban", "brand": "Eliquis", "class": "anticoagulant"},
    {"name": "rivaroxaban", "brand": "Xarelto", "class": "anticoagulant"},
    {"name": "fenofibrate", "brand": "Tricor", "class": "fibrate"},
    {"name": "ezetimibe", "brand": "Zetia", "class": "cholesterol"},
]

# GoodRx URL pattern
GOODRX_BASE_URL = "https://www.goodrx.com"

# User-Agent for scraping — we identify ourselves honestly
SCRAPER_HEADERS = {
    "User-Agent": "BeneFlex-PriceDiscovery/1.0 (healthcare price comparison; Constitution compliance)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SCRAPER_TIMEOUT = httpx.Timeout(15.0, read=30.0)


def scrape_goodrx_price(drug_name: str) -> dict:
    """Scrape the GoodRx.com page for a single drug and extract displayed prices.

    Returns a dict with:
        - drug_name: str
        - prices_found: list[dict] with pharmacy, price pairs
        - lowest_price: float or None
        - barrier: str or None (documents any access barrier)
        - url: str
    """
    url = f"{GOODRX_BASE_URL}/{drug_name}"
    result = {
        "drug_name": drug_name,
        "url": url,
        "prices_found": [],
        "lowest_price": None,
        "barrier": None,
        "scraped_at": datetime.now(UTC).isoformat(),
    }

    try:
        resp = httpx.get(
            url,
            headers=SCRAPER_HEADERS,
            timeout=SCRAPER_TIMEOUT,
            follow_redirects=True,
        )

        if resp.status_code == 403:
            result["barrier"] = (
                "HTTP 403 Forbidden — GoodRx blocked the request. "
                "Possible bot detection/WAF. Constitution documents this barrier: "
                "the pricing channel physically exists and is legally accessible "
                "(hiQ Labs v. LinkedIn, 2022), but technical measures prevent "
                "automated access at this time."
            )
            logger.warning(f"GoodRx 403 for {drug_name}: access blocked")
            return result

        if resp.status_code == 429:
            result["barrier"] = (
                "HTTP 429 Too Many Requests — rate limited by GoodRx. "
                "Channel exists and is legally accessible but temporarily throttled."
            )
            logger.warning(f"GoodRx 429 for {drug_name}: rate limited")
            return result

        if resp.status_code == 503:
            result["barrier"] = (
                "HTTP 503 Service Unavailable — GoodRx returned service error."
            )
            return result

        if resp.status_code != 200:
            result["barrier"] = f"HTTP {resp.status_code} — unexpected response"
            return result

        html = resp.text

        # Check for CAPTCHA/challenge page
        if "captcha" in html.lower() or "challenge" in html.lower() or "verify you are human" in html.lower():
            result["barrier"] = (
                "CAPTCHA/human verification detected on GoodRx page. "
                "Constitution documents this barrier: the pricing data physically "
                "exists on the page but is gated behind CAPTCHA that prevents "
                "automated comparison. Manual price checks remain possible."
            )
            logger.warning(f"GoodRx CAPTCHA for {drug_name}")
            return result

        # Check for JavaScript-only rendering
        if len(html) < 5000 and ("window.__NEXT_DATA__" in html or "react-root" in html.lower()):
            # Try to extract __NEXT_DATA__ JSON which contains pre-rendered data
            next_data_match = re.search(
                r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                html,
                re.DOTALL,
            )
            if next_data_match:
                try:
                    import json
                    next_data = json.loads(next_data_match.group(1))
                    prices = _extract_prices_from_next_data(next_data, drug_name)
                    if prices:
                        result["prices_found"] = prices
                        result["lowest_price"] = min(p["price"] for p in prices)
                        return result
                except Exception:
                    pass

        # Strategy 1: Extract from __NEXT_DATA__ (SSR React data)
        next_data_match = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if next_data_match:
            try:
                import json
                next_data = json.loads(next_data_match.group(1))
                prices = _extract_prices_from_next_data(next_data, drug_name)
                if prices:
                    result["prices_found"] = prices
                    result["lowest_price"] = min(p["price"] for p in prices)
                    return result
            except Exception as e:
                logger.debug(f"Failed to parse __NEXT_DATA__ for {drug_name}: {e}")

        # Strategy 2: Extract from structured data (JSON-LD)
        jsonld_matches = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        for jsonld_str in jsonld_matches:
            try:
                import json
                jsonld = json.loads(jsonld_str)
                prices = _extract_prices_from_jsonld(jsonld, drug_name)
                if prices:
                    result["prices_found"] = prices
                    result["lowest_price"] = min(p["price"] for p in prices)
                    return result
            except Exception:
                pass

        # Strategy 3: Regex extraction from HTML
        # GoodRx typically displays prices like "$4.00" or "$12.35"
        prices = _extract_prices_from_html(html, drug_name)
        if prices:
            result["prices_found"] = prices
            result["lowest_price"] = min(p["price"] for p in prices)
            return result

        # Strategy 4: Look for price data in meta tags
        meta_prices = re.findall(
            r'<meta[^>]*(?:content|value)="?\$?([\d,]+\.?\d*)"?[^>]*(?:price|cost|amount)',
            html,
            re.IGNORECASE,
        )
        if not meta_prices:
            meta_prices = re.findall(
                r'(?:price|cost|amount)[^>]*(?:content|value)="?\$?([\d,]+\.?\d*)"?',
                html,
                re.IGNORECASE,
            )
        for mp in meta_prices:
            try:
                price = float(mp.replace(",", ""))
                if 0.50 < price < 50000:  # reasonable drug price range
                    result["prices_found"].append({
                        "pharmacy": "GoodRx (meta)",
                        "price": price,
                        "source": "meta_tag",
                    })
            except ValueError:
                pass

        if result["prices_found"]:
            result["lowest_price"] = min(p["price"] for p in result["prices_found"])
        else:
            # Page loaded but no prices extractable — likely JS-rendered
            result["barrier"] = (
                "GoodRx page loaded (HTTP 200) but prices are rendered via "
                "client-side JavaScript and not present in the initial HTML. "
                "A headless browser (Playwright/Selenium) would be needed to "
                "extract dynamically rendered prices. Constitution documents "
                "this technical barrier: data physically exists on the page "
                "but requires JavaScript execution to access."
            )

    except httpx.TimeoutException:
        result["barrier"] = f"Timeout connecting to GoodRx for {drug_name}"
        logger.warning(f"GoodRx timeout for {drug_name}")
    except httpx.ConnectError as e:
        result["barrier"] = f"Connection error to GoodRx: {e}"
        logger.warning(f"GoodRx connection error for {drug_name}: {e}")
    except Exception as e:
        result["barrier"] = f"Unexpected error scraping GoodRx: {type(e).__name__}: {e}"
        logger.error(f"GoodRx scrape error for {drug_name}: {e}")

    return result


def _extract_prices_from_next_data(data: dict, drug_name: str) -> list[dict]:
    """Extract prices from GoodRx __NEXT_DATA__ JSON blob."""
    prices = []

    def _walk(obj, depth=0):
        if depth > 20:
            return
        if isinstance(obj, dict):
            # Look for price-related keys
            price_val = obj.get("price") or obj.get("lowPrice") or obj.get("retailPrice")
            if price_val is not None:
                try:
                    price = float(str(price_val).replace("$", "").replace(",", ""))
                    if 0.50 < price < 50000:
                        pharmacy = (
                            obj.get("pharmacyName")
                            or obj.get("pharmacy")
                            or obj.get("name")
                            or "GoodRx"
                        )
                        prices.append({
                            "pharmacy": str(pharmacy)[:100],
                            "price": price,
                            "source": "next_data",
                        })
                except (ValueError, TypeError):
                    pass
            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:200]:  # limit list traversal
                _walk(item, depth + 1)

    _walk(data)
    # Deduplicate by (pharmacy, price)
    seen = set()
    unique = []
    for p in prices:
        key = (p["pharmacy"], p["price"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _extract_prices_from_jsonld(data, drug_name: str) -> list[dict]:
    """Extract prices from JSON-LD structured data on GoodRx pages."""
    prices = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        offers = item.get("offers") or item.get("priceSpecification")
        if isinstance(offers, dict):
            offers = [offers]
        if isinstance(offers, list):
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                price_val = offer.get("price") or offer.get("lowPrice")
                if price_val is not None:
                    try:
                        price = float(str(price_val).replace("$", "").replace(",", ""))
                        if 0.50 < price < 50000:
                            seller = offer.get("seller", {})
                            pharmacy = (
                                seller.get("name") if isinstance(seller, dict) else None
                            ) or offer.get("name") or "GoodRx"
                            prices.append({
                                "pharmacy": str(pharmacy)[:100],
                                "price": price,
                                "source": "jsonld",
                            })
                    except (ValueError, TypeError):
                        pass
    return prices


def _extract_prices_from_html(html: str, drug_name: str) -> list[dict]:
    """Extract prices from GoodRx HTML using BeautifulSoup and regex."""
    prices = []

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        # Look for price elements with dollar amounts
        # GoodRx uses various class patterns for price display
        price_elements = soup.find_all(
            ["span", "div", "p", "strong"],
            string=re.compile(r'\$\s*\d+\.?\d*'),
        )
        for el in price_elements[:50]:  # limit
            text = el.get_text(strip=True)
            match = re.search(r'\$\s*([\d,]+\.?\d*)', text)
            if match:
                try:
                    price = float(match.group(1).replace(",", ""))
                    if 0.50 < price < 50000:
                        # Try to find nearby pharmacy name
                        parent = el.parent
                        pharmacy = "GoodRx"
                        if parent:
                            # Look for pharmacy name in siblings or parent text
                            siblings = parent.find_all(string=True, recursive=True)
                            for sib_text in siblings:
                                sib_text = str(sib_text).strip()
                                if sib_text and sib_text != text and len(sib_text) < 50:
                                    pharmacy_names = [
                                        "CVS", "Walgreens", "Walmart", "Rite Aid",
                                        "Costco", "Kroger", "Sam's Club", "Target",
                                        "Publix", "Safeway", "Albertsons",
                                    ]
                                    for pn in pharmacy_names:
                                        if pn.lower() in sib_text.lower():
                                            pharmacy = pn
                                            break
                        prices.append({
                            "pharmacy": pharmacy,
                            "price": price,
                            "source": "html_parse",
                        })
                except ValueError:
                    pass
    except ImportError:
        # Fallback to regex only
        pass

    # Also try raw regex on the HTML
    raw_prices = re.findall(r'\$\s*([\d,]+\.\d{2})', html)
    seen_prices = {p["price"] for p in prices}
    for rp in raw_prices[:100]:
        try:
            price = float(rp.replace(",", ""))
            if 0.50 < price < 50000 and price not in seen_prices:
                prices.append({
                    "pharmacy": "GoodRx (page)",
                    "price": price,
                    "source": "regex",
                })
                seen_prices.add(price)
        except ValueError:
            pass

    return prices


def scrape_goodrx_batch(
    db: Session,
    drugs: Optional[list[dict]] = None,
    max_drugs: int = 100,
) -> dict:
    """Scrape GoodRx prices for a batch of drugs and store results.

    Compares against NADAC prices already in the database.
    Records every comparison as structured data feeding F8.

    Returns a summary report.
    """
    if drugs is None:
        drugs = TOP_PRESCRIBED_DRUGS[:max_drugs]

    logger.info(f"Starting GoodRx scrape for {len(drugs)} drugs...")

    results = {
        "drugs_attempted": 0,
        "drugs_with_prices": 0,
        "drugs_blocked": 0,
        "prices_stored": 0,
        "comparisons_vs_nadac": 0,
        "goodrx_cheaper_count": 0,
        "nadac_cheaper_count": 0,
        "barriers": [],
        "price_comparisons": [],
        "scraped_at": datetime.now(UTC).isoformat(),
    }

    for drug_info in drugs:
        drug_name = drug_info["name"]
        results["drugs_attempted"] += 1

        scrape_result = scrape_goodrx_price(drug_name)

        if scrape_result["barrier"]:
            results["drugs_blocked"] += 1
            results["barriers"].append({
                "drug": drug_name,
                "barrier": scrape_result["barrier"],
                "url": scrape_result["url"],
            })

        if scrape_result["prices_found"]:
            results["drugs_with_prices"] += 1

            # Store each price in price_data
            for price_entry in scrape_result["prices_found"]:
                pd = PriceData(
                    provider_name=price_entry.get("pharmacy", "GoodRx"),
                    service_code=drug_name,  # Use drug name as service code for pharmacy
                    service_description=f"GoodRx discount price: {drug_info.get('brand', drug_name)}",
                    price=price_entry["price"],
                    channel=f"goodrx_{price_entry.get('pharmacy', 'unknown').lower().replace(' ', '_')}",
                    source=PriceSource.goodrx_scrape,
                    source_url=scrape_result["url"],
                    ingested_at=datetime.now(UTC),
                )
                db.add(pd)
                results["prices_stored"] += 1

            # Compare against NADAC
            nadac_price = _get_nadac_price_for_drug(db, drug_name)
            goodrx_lowest = scrape_result["lowest_price"]

            comparison = {
                "drug": drug_name,
                "brand": drug_info.get("brand"),
                "goodrx_lowest": goodrx_lowest,
                "nadac_price": nadac_price,
                "goodrx_url": scrape_result["url"],
                "pharmacies_compared": len(scrape_result["prices_found"]),
            }

            if nadac_price is not None and goodrx_lowest is not None:
                results["comparisons_vs_nadac"] += 1
                if goodrx_lowest < nadac_price:
                    results["goodrx_cheaper_count"] += 1
                    comparison["cheaper_channel"] = "goodrx"
                    comparison["savings"] = round(nadac_price - goodrx_lowest, 2)
                else:
                    results["nadac_cheaper_count"] += 1
                    comparison["cheaper_channel"] = "nadac"
                    comparison["savings"] = round(goodrx_lowest - nadac_price, 2)

            results["price_comparisons"].append(comparison)

    # Commit all stored prices
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to commit GoodRx prices: {e}")

    # Store a pipeline metric for F8
    _record_goodrx_pipeline_metric(db, results)

    logger.info(
        f"GoodRx scrape complete: {results['drugs_with_prices']}/{results['drugs_attempted']} "
        f"with prices, {results['drugs_blocked']} blocked, "
        f"{results['prices_stored']} prices stored"
    )

    return results


def _get_nadac_price_for_drug(db: Session, drug_name: str) -> Optional[float]:
    """Look up the NADAC price for a drug by name (fuzzy match on service_description)."""
    # Try exact match on service_code first
    row = db.query(PriceData.price).filter(
        PriceData.source == PriceSource.nadac_pharmacy,
        PriceData.service_code == drug_name,
    ).first()
    if row:
        return float(row.price)

    # Try fuzzy match on description
    row = db.query(PriceData.price).filter(
        PriceData.source == PriceSource.nadac_pharmacy,
        PriceData.service_description.ilike(f"%{drug_name}%"),
    ).order_by(PriceData.price.asc()).first()
    if row:
        return float(row.price)

    return None


def _record_goodrx_pipeline_metric(db: Session, results: dict):
    """Record GoodRx scraping results as a data pipeline metric for F8."""
    from app.models.data_pipeline_metric import DataPipelineMetric

    metric = DataPipelineMetric(
        metric_type="goodrx_price_scrape",
        benefit_type="pharmacy",
        value=float(results["drugs_with_prices"]),
        details={
            "drugs_attempted": results["drugs_attempted"],
            "drugs_with_prices": results["drugs_with_prices"],
            "drugs_blocked": results["drugs_blocked"],
            "prices_stored": results["prices_stored"],
            "comparisons_vs_nadac": results["comparisons_vs_nadac"],
            "goodrx_cheaper_count": results["goodrx_cheaper_count"],
            "nadac_cheaper_count": results["nadac_cheaper_count"],
            "barriers_summary": [b["barrier"][:200] for b in results["barriers"][:10]],
        },
        measured_at=datetime.now(UTC),
    )
    db.add(metric)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record GoodRx metric: {e}")
