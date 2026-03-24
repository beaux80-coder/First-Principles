"""Aggregate anonymized research publications (Function 6A).

Constitution requirement: "Publishes aggregate, anonymized findings regularly
as industry research (e.g., average employer overpayment by industry and
geography, employee experience gaps by company size). Published data is limited
to aggregate conclusions — no proprietary data (verified prices, provider
scores, outcome models, negotiated rates) is ever exposed."

This module generates publishable research insights from the data pipeline
without exposing any proprietary data.
"""

from datetime import datetime, UTC

from sqlalchemy import func, and_, text
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource
from app.models.benchmark_query import BenchmarkQuery


def generate_research_report(db: Session) -> dict:
    """Generate aggregate anonymized research findings.

    Published insights include:
    - Average price variations by geography (from public transparency data only)
    - Benchmark query patterns (anonymized, aggregated)
    - Benefit cost structure insights
    - Care access patterns by geography

    NO proprietary data: no verified prices, no provider scores, no outcome
    models, no negotiated rates, no employer-specific data.

    Optimized for large datasets (11M+ rows) using sampled averages
    and approximate counts to avoid full-table scans.
    """
    from sqlalchemy import text

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "methodology": "All findings derived from publicly available CMS data and anonymized, "
                       "aggregated benchmark queries. No proprietary data is included.",
        "findings": [],
    }

    # 1. Geographic price variation (sampled AVG for speed)
    sample_subq = (
        db.query(PriceData.state, PriceData.price)
        .filter(
            and_(
                PriceData.source == PriceSource.hospital_transparency,
                PriceData.channel == "cash",
                PriceData.state.isnot(None),
            )
        )
        .limit(200_000)
        .subquery()
    )
    state_avg_prices = dict(
        db.query(sample_subq.c.state, func.avg(sample_subq.c.price))
        .group_by(sample_subq.c.state)
        .all()
    )

    if state_avg_prices:
        prices = sorted(state_avg_prices.items(), key=lambda x: x[1] if x[1] else 0, reverse=True)
        national_avg = sum(p for _, p in prices if p) / len([p for _, p in prices if p]) if prices else 0

        most_expensive = [(s, round(float(p), 2)) for s, p in prices[:5] if p]
        least_expensive = [(s, round(float(p), 2)) for s, p in prices[-5:] if p]

        report["findings"].append({
            "title": "Hospital Cash Price Variation by State",
            "category": "pricing",
            "insight": f"Average hospital cash prices vary {round(float(prices[0][1]) / float(prices[-1][1]), 1) if prices[-1][1] and prices[0][1] else 'N/A'}x "
                       f"between the most and least expensive states.",
            "data": {
                "national_average": round(float(national_avg), 2),
                "most_expensive_states": most_expensive,
                "least_expensive_states": least_expensive,
                "states_analyzed": len(state_avg_prices),
            },
            "source": "CMS Hospital Price Transparency files (public)",
        })

    # 2. Medicare vs hospital price gap (sampled AVGs)
    medicare_subq = (
        db.query(PriceData.price)
        .filter(PriceData.source == PriceSource.medicare_physician_fee)
        .limit(100_000)
        .subquery()
    )
    medicare_avg = db.query(func.avg(medicare_subq.c.price)).scalar()

    hospital_subq = (
        db.query(PriceData.price)
        .filter(
            and_(
                PriceData.source == PriceSource.hospital_transparency,
                PriceData.channel == "cash",
            )
        )
        .limit(200_000)
        .subquery()
    )
    hospital_national_avg = db.query(func.avg(hospital_subq.c.price)).scalar()

    if medicare_avg and hospital_national_avg:
        ratio = float(hospital_national_avg) / float(medicare_avg)
        report["findings"].append({
            "title": "Hospital Cash Prices vs Medicare Rates",
            "category": "pricing",
            "insight": f"Average hospital cash prices are {ratio:.1f}x Medicare rates nationally. "
                       f"This gap represents the price discovery opportunity for self-funded plans.",
            "data": {
                "hospital_avg": round(float(hospital_national_avg), 2),
                "medicare_avg": round(float(medicare_avg), 2),
                "ratio": round(ratio, 2),
            },
            "source": "CMS Hospital Price Transparency files and Medicare Physician Fee Schedule (public)",
        })

    # 3. Pharmacy cost concentration (approximate distinct counts)
    total_drugs = db.execute(text(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT service_code FROM price_data"
        "  WHERE source = 'nadac_pharmacy' LIMIT 50000"
        ")"
    )).scalar() or 0

    high_cost_drugs = db.execute(text(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT service_code FROM price_data"
        "  WHERE source = 'nadac_pharmacy' AND price > 100 LIMIT 50000"
        ")"
    )).scalar() or 0

    if total_drugs > 0:
        report["findings"].append({
            "title": "Pharmacy Cost Concentration",
            "category": "pharmacy",
            "insight": f"{round(high_cost_drugs / total_drugs * 100, 1)}% of drug formulations "
                       f"have acquisition costs above $100/unit. PBM spread pricing "
                       f"on these high-cost drugs represents the largest pharmacy savings opportunity.",
            "data": {
                "total_drug_formulations": total_drugs,
                "high_cost_formulations": high_cost_drugs,
                "high_cost_pct": round(high_cost_drugs / total_drugs * 100, 1),
            },
            "source": "CMS NADAC (National Average Drug Acquisition Cost) — public",
        })

    # 4. Benchmark query volume (small table, no optimization needed)
    total_queries = db.query(func.count(BenchmarkQuery.query_id)).scalar() or 0
    if total_queries > 0:
        avg_employee_count = db.query(func.avg(
            func.json_extract(BenchmarkQuery.inputs, "$.employee_count")
        )).scalar()

        report["findings"].append({
            "title": "Benchmark Tool Usage",
            "category": "market_interest",
            "insight": f"{total_queries} benchmark analyses generated. "
                       f"Average employer size: {round(float(avg_employee_count))} employees." if avg_employee_count else
                       f"{total_queries} benchmark analyses generated.",
            "data": {
                "total_queries": total_queries,
            },
            "source": "Anonymized, aggregated benchmark queries",
        })

    # 5. Data pipeline coverage (approximate counts)
    total_records = db.execute(text("SELECT MAX(rowid) FROM price_data")).scalar() or 0
    unique_services = db.execute(text(
        "SELECT COUNT(*) FROM (SELECT DISTINCT service_code FROM price_data LIMIT 50000)"
    )).scalar() or 0
    states_covered = db.execute(text(
        "SELECT COUNT(DISTINCT state) FROM price_data WHERE state IS NOT NULL"
    )).scalar() or 0

    report["findings"].append({
        "title": "Public Pricing Data Coverage",
        "category": "data_pipeline",
        "insight": f"The system analyzes {total_records:,} price data points "
                   f"across {unique_services:,} unique services in {states_covered} states. "
                   f"Data sources: CMS hospital transparency files, Medicare fee schedules, "
                   f"NADAC pharmacy pricing, and insurer transparency in coverage files.",
        "data": {
            "total_records": total_records,
            "unique_services": unique_services,
            "states_covered": states_covered,
        },
        "source": "CMS and insurer public data (aggregated)",
    })

    report["disclaimer"] = (
        "All findings are derived from publicly available data published by CMS and "
        "insurers as required by federal transparency regulations. No proprietary data, "
        "verified prices, provider scores, outcome models, or employer-specific information "
        "is included in this report. Aggregate conclusions only."
    )

    return report
